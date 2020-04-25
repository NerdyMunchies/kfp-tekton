# Copyright 2019-2020 kubeflow.org.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from collections import OrderedDict

from kfp.compiler._k8s_helper import convert_k8s_obj_to_json
from kfp.compiler._op_to_template import _process_obj, _inputs_to_json, _outputs_to_json
from kfp import dsl
from kfp.dsl._container_op import BaseOp
from typing import List, Text, Dict, Any
from kfp.dsl import ArtifactLocation
import textwrap
import yaml
import re

from .. import tekton_api_version


def _get_base_step(name: str):
    """Base image step for running bash commands.

    Return a busybox base step for running bash commands.

    Args:
        name {str}: step name

    Returns:
        Dict[Text, Any]
    """
    return {
        'image': 'busybox',
        'name': name,
        'script': '#!/bin/sh\nset -exo pipefail\n'
    }


def _get_resourceOp_template(op: BaseOp,
                             name: str,
                             tekton_api_version: str,
                             resource_manifest: str,
                             argo_var: bool = False):
    """Tekton task template for running resourceOp

    Return a Tekton task template for interacting Kubernetes resources.

    Args:
        op {BaseOp}: class that inherits from BaseOp
        name {str}: resourceOp name
        tekton_api_version {str}: Tekton API Version
        resource_manifest {str}: Kubernetes manifest file for deploying resources.
        argo_var {bool}: Check whether Argo variable replacement is necessary.

    Returns:
        Dict[Text, Any]
    """
    template = {
        'apiVersion': tekton_api_version,
        'kind': 'Task',
        'metadata': {'name': name},
        'spec': {
            "params": [
                {
                    "description": "Action on the resource",
                    "name": "action",
                    "type": "string"
                },
                {
                    "default": "strategic",
                    "description": "Merge strategy when using action patch",
                    "name": "merge-strategy",
                    "type": "string"
                },
                {
                    "default": "",
                    "description": "An express to retrieval data from resource.",
                    "name": "output",
                    "type": "string"
                },
                {
                    "default": "",
                    "description": "A label selector express to decide if the action on resource is success.",
                    "name": "success-condition",
                    "type": "string"
                },
                {
                    "default": "",
                    "description": "A label selector express to decide if the action on resource is failure.",
                    "name": "failure-condition",
                    "type": "string"
                },
                {
                    # Todo: The image need to be replaced, once there are official images from tekton
                    "default": "index.docker.io/aipipeline/kubeclient:v0.0.2",
                    "description": "Kubectl wrapper image",
                    "name": "image",
                    "type": "string"
                },
                {
                    "default": "false",
                    "description": "Enable set owner reference for created resource.",
                    "name": "set-ownerreference",
                    "type": "string"
                }
            ],
            'steps': [
                {
                    "args": [
                        "--action=$(params.action)",
                        "--merge-strategy=$(params.merge-strategy)",
                        "--manifest=%s" % resource_manifest,
                        "--output=$(params.output)",
                        "--success-condition=$(params.success-condition)",
                        "--failure-condition=$(params.failure-condition)",
                        "--set-ownerreference=$(params.set-ownerreference)"
                    ],
                    "image": "$(params.image)",
                    "name": name,
                    "resources": {}
                }
            ]
        }
    }

    # Inject Argo variable replacement as env variables.
    if argo_var:
        template['spec']['steps'][0]['env'] = [
            {'name': 'PIPELINERUN', 'valueFrom': {'fieldRef': {'fieldPath': "metadata.labels['tekton.dev/pipelineRun']"}}}
        ]

    # Add results if exist.
    if op.attribute_outputs.items():
        template['spec']['results'] = []
        for output_item in sorted(list(op.attribute_outputs.items()), key=lambda x: x[0]):
            template['spec']['results'].append({'name': output_item[0], 'description': output_item[1]})

    return template


def _add_mount_path(name: str,
                    path: str,
                    mount_path: str,
                    volume_mount_step_template: List[Dict[Text, Any]],
                    volume_template: List[Dict[Text, Any]],
                    mounted_param_paths: List[Text]):
    """
    Add emptyDir to the given mount_path for persisting files within the same tasks
    """
    volume_mount_step_template.append({'name': name, 'mountPath': path.rsplit("/", 1)[0]})
    volume_template.append({'name': name, 'emptyDir': {}})
    mounted_param_paths.append(mount_path)


def _update_volumes(template: Dict[Text, Any],
                    volume_mount_step_template: List[Dict[Text, Any]],
                    volume_template: List[Dict[Text, Any]]):
    """
    Update the list of volumes and volumeMounts on a given template
    """
    if volume_mount_step_template:
        template['spec']['stepTemplate'] = {}
        template['spec']['stepTemplate']['volumeMounts'] = volume_mount_step_template
        template['spec']['volumes'] = volume_template


def _prepend_steps(prep_steps: List[Dict[Text, Any]], original_steps: List[Dict[Text, Any]]):
    """
    Prepend steps to the original step list.
    """
    steps = prep_steps.copy()
    steps.extend(original_steps)
    return steps


def _process_parameters(processed_op: BaseOp,
                        template: Dict[Text, Any],
                        outputs_dict: Dict[Text, Any],
                        volume_mount_step_template: List[Dict[Text, Any]],
                        volume_template: List[Dict[Text, Any]],
                        replaced_param_list: List[Text],
                        artifact_to_result_mapping: Dict[Text, Any],
                        mounted_param_paths: List[Text]):
    """Process output parameters to replicate the same behavior as Argo.

    Since Tekton results need to be under /tekton/results. If file output paths cannot be
    configured to /tekton/results, we need to create the below copy step for moving
    file outputs to the Tekton destination. BusyBox is recommended to be used on
    small tasks because it's relatively lightweight and small compared to the ubuntu and
    bash images.

    - image: busybox
        name: copy-results
        script: |
            #!/bin/sh
            set -exo pipefail
            cp $LOCALPATH $(results.data.path);

    Args:
        processed_op {BaseOp}: class that inherits from BaseOp
        template {Dict[Text, Any]}: Task template
        outputs_dict {Dict[Text, Any]}: Dictionary of the possible parameters/artifacts in this task
        volume_mount_step_template {List[Dict[Text, Any]]}: Step template for the list of volume mounts
        volume_template {List[Dict[Text, Any]]}: Task template for the list of volumes
        replaced_param_list {List[Text]}: List of parameters that already set up as results
        artifact_to_result_mapping {Dict[Text, Any]}: Mapping between parameter and artifact results
        mounted_param_paths {List[Text]}: List of paths that already mounted to a volume.

    Returns:
        Dict[Text, Any]
    """
    if outputs_dict.get('parameters'):
        template['spec']['results'] = []
        copy_results_step = _get_base_step('copy-results')
        for name, path in processed_op.file_outputs.items():
            name = name.replace('_', '-')  # replace '_' to '-' since tekton results doesn't support underscore
            template['spec']['results'].append({
                'name': name,
                'description': path
            })
            # replace all occurrences of the output file path with the Tekton output parameter expression
            need_copy_step = True
            for s in template['spec']['steps']:
                if 'command' in s:
                    commands = []
                    for c in s['command']:
                        if path in c:
                            c = c.replace(path, '$(results.%s.path)' % name)
                            need_copy_step = False
                        commands.append(c)
                    s['command'] = commands
                if 'args' in s:
                    args = []
                    for a in s['args']:
                        if path in a:
                            a = a.replace(path, '$(results.%s.path)' % name)
                            need_copy_step = False
                        args.append(a)
                    s['args'] = args
            # If file output path cannot be found/replaced, use emptyDir to copy it to the tekton/results path
            if need_copy_step:
                copy_results_step['script'] = copy_results_step['script'] + 'cp ' + path + ' $(results.%s.path);' % name + '\n'
                mount_path = path.rsplit("/", 1)[0]
                if mount_path not in mounted_param_paths:
                    _add_mount_path(name, path, mount_path, volume_mount_step_template, volume_template, mounted_param_paths)
            # Record what artifacts are moved to result parameters.
            parameter_name = (processed_op.name + '-' + name).replace(' ', '-').replace('_', '-')
            replaced_param_list.append(parameter_name)
            artifact_to_result_mapping[parameter_name] = name
        return copy_results_step
    else:
        return {}


def _process_output_artifacts(outputs_dict: Dict[Text, Any],
                              volume_mount_step_template: List[Dict[Text, Any]],
                              volume_template: List[Dict[Text, Any]],
                              replaced_param_list: List[Text],
                              artifact_to_result_mapping: Dict[Text, Any]):
    """Process output artifacts to replicate the same behavior as Argo.

    For storing artifacts, we will be using the minio/mc image because we need to upload artifacts to any type of
    object storage and endpoint. The minio/mc is the best image suited for this task because the default KFP
    is using minio and it also works well with other s3/gcs type of storage. 

    - image: minio/mc
        name: copy-artifacts
        script: |
            #!/usr/bin/env sh
            mc config host add storage http://minio-service.$NAMESPACE:9000 $AWS_ACCESS_KEY_ID $AWS_SECRET_ACCESS_KEY
            mc cp /tmp/file.txt storage/$(inputs.params.bucket)/runs/$PIPELINERUN/$PODNAME/file.txt

    Args:
        outputs_dict {Dict[Text, Any]}: Dictionary of the possible parameters/artifacts in this task
        volume_mount_step_template {List[Dict[Text, Any]]}: Step template for the list of volume mounts
        volume_template {List[Dict[Text, Any]]}: Task template for the list of volumes
        replaced_param_list {List[Text]}: List of parameters that already set up as results
        artifact_to_result_mapping {Dict[Text, Any]}: Mapping between parameter and artifact results

    Returns:
        Dict[Text, Any]
    """
    if outputs_dict.get('artifacts'):
        # TODO: Pull default values from KFP configmap when integrated with KFP.
        storage_location = outputs_dict['artifacts'][0].get('s3', {})
        insecure = storage_location.get("insecure", True)
        endpoint = storage_location.get("endpoint", "minio-service.$NAMESPACE:9000")
        # We want to use the insecure flag to figure out whether to use http or https scheme
        endpoint = re.sub(r"https?://", "", endpoint)
        endpoint = 'http://' + endpoint if insecure else 'https://' + endpoint
        access_key = storage_location.get("accessKeySecret", {"name": "mlpipeline-minio-artifact", "key": "accesskey"})
        secret_access_key = storage_location.get("secretKeySecret", {"name": "mlpipeline-minio-artifact", "key": "secretkey"})
        bucket = storage_location.get("bucket", "mlpipeline")
        copy_artifacts_step = {
            'image': 'minio/mc',
            'name': 'copy-artifacts',
            'script': textwrap.dedent('''\
                        #!/usr/bin/env sh
                        mc config host add storage %s $AWS_ACCESS_KEY_ID $AWS_SECRET_ACCESS_KEY
                        ''' % (endpoint)),
            'env': [
                {'name': 'PIPELINERUN', 'valueFrom': {'fieldRef': {'fieldPath': "metadata.labels['tekton.dev/pipelineRun']"}}},
                {'name': 'PODNAME', 'valueFrom': {'fieldRef': {'fieldPath': "metadata.name"}}},
                {'name': 'NAMESPACE', 'valueFrom': {'fieldRef': {'fieldPath': "metadata.namespace"}}},
                {'name': 'AWS_ACCESS_KEY_ID', 'valueFrom': {'secretKeyRef': {'name': access_key['name'], 'key': access_key['key']}}},
                {'name': 'AWS_SECRET_ACCESS_KEY', 'valueFrom': {'secretKeyRef': {'name': secret_access_key['name'], 'key': secret_access_key['key']}}}
            ]
        }
        mounted_artifact_paths = []
        for artifact in outputs_dict['artifacts']:
            if artifact['name'] in replaced_param_list:
                copy_artifacts_step['script'] = copy_artifacts_step['script'] + \
                    'mc cp $(results.%s.path) storage/%s/runs/$PIPELINERUN/$PODNAME/%s\n' % (artifact_to_result_mapping[artifact['name']], bucket, artifact['path'].rsplit("/", 1)[1])
            else:
                copy_artifacts_step['script'] = copy_artifacts_step['script'] + \
                    'mc cp %s storage/%s/runs/$PIPELINERUN/$PODNAME/%s\n' % (artifact['path'], bucket, artifact['path'].rsplit("/", 1)[1])
                if artifact['path'].rsplit("/", 1)[0] not in mounted_artifact_paths:
                    volume_mount_step_template.append({'name': artifact['name'], 'mountPath': artifact['path'].rsplit("/", 1)[0]})
                    volume_template.append({'name': artifact['name'], 'emptyDir': {}})
                    mounted_artifact_paths.append(artifact['path'].rsplit("/", 1)[0])
        return copy_artifacts_step
    else:
        return {}


def _process_base_ops(op: BaseOp):
    """Recursively go through the attrs listed in `attrs_with_pipelineparams`
    and sanitize and replace pipeline params with template var string.

    Returns a processed `BaseOp`.

    NOTE this is an in-place update to `BaseOp`'s attributes (i.e. the ones
    specified in `attrs_with_pipelineparams`, all `PipelineParam` are replaced
    with the corresponding template variable strings).

    Args:
        op {BaseOp}: class that inherits from BaseOp

    Returns:
        BaseOp
    """

    # map param's (unsanitized pattern or serialized str pattern) -> input param var str
    map_to_tmpl_var = {
        (param.pattern or str(param)): '$(inputs.params.%s)' % param.full_name  # Tekton change
        for param in op.inputs
    }

    # process all attr with pipelineParams except inputs and outputs parameters
    for key in op.attrs_with_pipelineparams:
        setattr(op, key, _process_obj(getattr(op, key), map_to_tmpl_var))

    return op


def _op_to_template(op: BaseOp, enable_artifacts=False):
    """Generate template given an operator inherited from BaseOp."""

    # initial local variables for tracking volumes and artifacts
    volume_mount_step_template = []
    volume_template = []
    mounted_param_paths = []
    replaced_param_list = []
    artifact_to_result_mapping = {}

    # NOTE in-place update to BaseOp
    # replace all PipelineParams with template var strings
    processed_op = _process_base_ops(op)

    if isinstance(op, dsl.ContainerOp):
        # default output artifacts
        output_artifact_paths = OrderedDict(op.output_artifact_paths)
        # print(op.output_artifact_paths)
        # This should have been as easy as output_artifact_paths.update(op.file_outputs),
        # but the _outputs_to_json function changes the output names and we must do the same here,
        # so that the names are the same
        output_artifact_paths.update(sorted(((param.full_name, processed_op.file_outputs[param.name]) for param in processed_op.outputs.values()), key=lambda x: x[0]))

        output_artifacts = [
            convert_k8s_obj_to_json(
                ArtifactLocation.create_artifact_for_s3(
                    op.artifact_location,
                    name=name,
                    path=path,
                    key='runs/$PIPELINERUN/$PODNAME/' + name))
            for name, path in output_artifact_paths.items()
        ] if enable_artifacts else []

        # workflow template
        container = convert_k8s_obj_to_json(
            processed_op.container
        )

        step = {'name': processed_op.name}
        step.update(container)

        template = {
            'apiVersion': tekton_api_version,
            'kind': 'Task',
            'metadata': {'name': processed_op.name},
            'spec': {
                'steps': [step]
            }
        }

    elif isinstance(op, dsl.ResourceOp):
        # no output artifacts
        output_artifacts = []

        # Flatten manifest because it needs to replace Argo variables
        manifest = yaml.dump(convert_k8s_obj_to_json(processed_op.k8s_resource), default_flow_style=False)
        argo_var = False
        if manifest.find('{{workflow.name}}') != -1:
            # Kubernetes Pod arguments only take $() as environment variables
            manifest = manifest.replace('{{workflow.name}}', "$(PIPELINERUN)")
            # Remove yaml quote in order to read bash variables
            manifest = re.sub('name: \'([^\']+)\'', 'name: \g<1>', manifest)
            argo_var = True

        # task template
        template = _get_resourceOp_template(op, processed_op.name, tekton_api_version, manifest, argo_var=argo_var)

    # initContainers
    if processed_op.init_containers:
        template['spec']['steps'] = _prepend_steps(processed_op.init_containers, template['spec']['steps'])

    # inputs
    input_artifact_paths = processed_op.input_artifact_paths if isinstance(processed_op, dsl.ContainerOp) else None
    artifact_arguments = processed_op.artifact_arguments if isinstance(processed_op, dsl.ContainerOp) else None
    inputs = _inputs_to_json(processed_op.inputs, input_artifact_paths, artifact_arguments)
    if 'parameters' in inputs:
        if isinstance(processed_op, dsl.ContainerOp):
            template['spec']['params'] = inputs['parameters']
        elif isinstance(op, dsl.ResourceOp):
            template['spec']['params'].extend(inputs['parameters'])
    if 'artifacts' in inputs:
        # The input artifacts in KFP is not pulling from s3, it will always be passed as a raw input.
        # Visit https://github.com/kubeflow/pipelines/issues/336 for more details on the implementation.
        copy_inputs_step = _get_base_step('copy-inputs')
        for artifact in inputs['artifacts']:
            if 'raw' in artifact:
                copy_inputs_step['script'] += 'echo -n "%s" > %s\n' % (artifact['raw']['data'], artifact['path'])
            mount_path = artifact['path'].rsplit("/", 1)[0]
            if mount_path not in mounted_param_paths:
                _add_mount_path(artifact['name'], artifact['path'], mount_path, volume_mount_step_template, volume_template, mounted_param_paths)
        template['spec']['steps'] = _prepend_steps([copy_inputs_step], template['spec']['steps'])
        _update_volumes(template, volume_mount_step_template, volume_template)

    # outputs
    if isinstance(op, dsl.ContainerOp):
        op_outputs = processed_op.outputs
        param_outputs = processed_op.file_outputs
    elif isinstance(op, dsl.ResourceOp):
        op_outputs = {}
        param_outputs = {}
    outputs_dict = _outputs_to_json(op, op_outputs, param_outputs, output_artifacts)
    if outputs_dict:
        copy_results_step = _process_parameters(processed_op,
                                                template,
                                                outputs_dict,
                                                volume_mount_step_template,
                                                volume_template,
                                                replaced_param_list,
                                                artifact_to_result_mapping,
                                                mounted_param_paths)
        copy_artifacts_step = _process_output_artifacts(outputs_dict,
                                                        volume_mount_step_template,
                                                        volume_template,
                                                        replaced_param_list,
                                                        artifact_to_result_mapping)
        if mounted_param_paths:
            template['spec']['steps'].append(copy_results_step)
        _update_volumes(template, volume_mount_step_template, volume_template)
        if copy_artifacts_step:
            template['spec']['steps'].append(copy_artifacts_step)

    # **********************************************************
    #  NOTE: the following features are still under development
    # **********************************************************

    # metadata
    if processed_op.pod_annotations or processed_op.pod_labels:
        template.setdefault('metadata', {})  # Tekton change, don't wipe out existing metadata
        if processed_op.pod_annotations:
            template['metadata']['annotations'] = processed_op.pod_annotations
        if processed_op.pod_labels:
            template['metadata']['labels'] = processed_op.pod_labels

    # sidecars
    if processed_op.sidecars:
        template['spec']['sidecars'] = processed_op.sidecars

    # volumes
    if processed_op.volumes:
        template['spec']['volumes'] = template['spec'].get('volume', []) + [convert_k8s_obj_to_json(volume) for volume in processed_op.volumes]
        template['spec']['volumes'].sort(key=lambda x: x['name'])

    # Display name
    if processed_op.display_name:
        template.setdefault('metadata', {}).setdefault('annotations', {})['pipelines.kubeflow.org/task_display_name'] = processed_op.display_name

    if isinstance(op, dsl.ContainerOp) and op._metadata:
        import json
        template.setdefault('metadata', {}).setdefault('annotations', {})['pipelines.kubeflow.org/component_spec'] = json.dumps(op._metadata.to_dict(), sort_keys=True)

    return template
