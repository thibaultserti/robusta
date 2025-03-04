import logging

from robusta.api import (
    ActionException,
    DeploymentEvent,
    ErrorCodes,
    EventChangeEvent,
    EventEnricherParams,
    ExecutionBaseEvent,
    Finding,
    FindingSeverity,
    FindingSource,
    FindingSubject,
    FindingSubjectType,
    FindingType,
    KubeObjFindingSubject,
    KubernetesResourceEvent,
    MarkdownBlock,
    PodEvent,
    RendererType,
    SlackAnnotations,
    TableBlock,
    VideoEnricherParams,
    VideoLink,
    action,
    get_event_timestamp,
    get_job_all_pods,
    is_pod_finished,
    get_resource_events,
    get_resource_events_table,
    list_pods_using_selector,
    parse_kubernetes_datetime_to_ms,
    KubernetesAnyChangeEvent,
    extract_ready_pods,
    is_release_managed_by_helm,
    extract_total_pods,
    ServiceConfig,
    VolumeInfo,
    ContainerInfo,
    extract_volumes_k8,
    extract_containers_k8,
    Pod,
    ReplicaSet,
    StatefulSet,
    DaemonSet,
    Deployment,
    ServiceInfo
)

class ExtendedEventEnricherParams(EventEnricherParams):
    """
    :var dependent_pod_mode: when True, instead of fetching events for the deployment itself, fetch events for pods in the deployment.
    """

    dependent_pod_mode: bool = False
    max_pods: int = 1


@action
def event_report(event: EventChangeEvent):
    """
    Create finding based on the kubernetes event
    """
    k8s_obj = event.obj.regarding

    # creating the finding before the rate limiter, to use the service key for rate limiting
    finding = Finding(
        title=f"{event.obj.reason} {event.obj.type} for {k8s_obj.kind} {k8s_obj.namespace}/{k8s_obj.name}",
        description=event.obj.note,
        source=FindingSource.KUBERNETES_API_SERVER,
        severity=FindingSeverity.INFO if event.obj.type == "Normal" else FindingSeverity.DEBUG,
        finding_type=FindingType.ISSUE,
        aggregation_key=f"Kubernetes {event.obj.type} Event",
        subject=FindingSubject(
            name=k8s_obj.name,
            subject_type=FindingSubjectType.from_kind(k8s_obj.kind),
            namespace=k8s_obj.namespace,
            node=KubeObjFindingSubject.get_node_name(k8s_obj),
        ),
    )
    event.add_finding(finding)


@action
def event_resource_events(event: EventChangeEvent):
    """
    Given a Kubernetes event, gather all other events on the same resource in the near past
    """
    if not event.get_event():
        logging.error(f"cannot run event_resource_events on alert with no events object: {event}")
        return
    obj = event.obj.regarding
    events_table = get_resource_events_table(
        "*Related Events*",
        obj.kind,
        obj.name,
        obj.namespace,
    )
    if events_table:
        event.add_enrichment(
            [events_table],
            {SlackAnnotations.ATTACHMENT: True},
        )


@action
def resource_events_enricher(event: KubernetesResourceEvent, params: ExtendedEventEnricherParams):
    """
    Given a Kubernetes resource, fetch related events in the near past
    """

    resource = event.get_resource()
    if resource.kind not in ["Pod", "Deployment", "DaemonSet", "ReplicaSet", "StatefulSet", "Job", "Node"]:
        raise ActionException(
            ErrorCodes.RESOURCE_NOT_SUPPORTED, f"Resource events enricher is not supported for resource {resource.kind}"
        )

    kind: str = resource.kind

    events = get_resource_events(
        kind,
        resource.metadata.name,
        resource.metadata.namespace,
        included_types=params.included_types,
    )

    # append related pod data as well
    if params.dependent_pod_mode and kind in ["Deployment", "DaemonSet", "ReplicaSet", "StatefulSet", "Job"]:
        pods = []
        if kind == "Job":
            pods = get_job_all_pods(resource) or []
        else:
            pods = list_pods_using_selector(resource.metadata.namespace, resource.spec.selector, "")

        selected_pods = pods[: min(len(pods), params.max_pods)]
        for pod in selected_pods:
            if len(events) >= params.max_events:
                break

            events.extend(
                get_resource_events(
                    "Pod",
                    pod.metadata.name,
                    pod.metadata.namespace,
                    included_types=params.included_types,
                )
            )

    events = events[: params.max_events]
    events = sorted(events, key=get_event_timestamp, reverse=True)

    if len(events) > 0:
        rows = [
            [
                e.reason,
                e.type,
                parse_kubernetes_datetime_to_ms(get_event_timestamp(e)) if get_event_timestamp(e) else 0,
                e.regarding.kind,
                e.regarding.name,
                e.note,
            ]
            for e in events
        ]

        event.add_enrichment(
            [
                TableBlock(
                    table_name=f"*{kind} events:*",
                    column_renderers={"time": RendererType.DATETIME},
                    headers=["reason", "type", "time", "kind", "name", "message"],
                    rows=rows,
                    column_width=[1, 1, 1, 1, 1, 2],
                )
            ],
            {SlackAnnotations.ATTACHMENT: True},
        )


@action
def pod_events_enricher(event: PodEvent, params: EventEnricherParams):
    """
    Given a Kubernetes pod, fetch related events in the near past
    """
    pod = event.get_pod()
    if not pod:
        logging.error(f"cannot run pod_events_enricher on alert with no pod object: {event}")
        return

    events_table_block = get_resource_events_table(
        "*Pod events:*",
        pod.kind,
        pod.metadata.name,
        pod.metadata.namespace,
        included_types=params.included_types,
        max_events=params.max_events,
    )
    if events_table_block:
        event.add_enrichment([events_table_block], {SlackAnnotations.ATTACHMENT: True})


@action
def deployment_events_enricher(event: DeploymentEvent, params: ExtendedEventEnricherParams):
    """
    Given a deployment, fetch related events in the near past.

    Can optionally fetch events for related pods instead.
    """
    dep = event.get_deployment()
    if not dep:
        logging.error(f"cannot run deployment_events_enricher on alert with no deployment object: {event}")
        return

    if params.dependent_pod_mode:
        pods = list_pods_using_selector(dep.metadata.namespace, dep.spec.selector, "status.phase!=Running")
        if pods:
            selected_pods = pods if len(pods) <= params.max_pods else pods[: params.max_pods]
            for pod in selected_pods:
                events_table_block = get_resource_events_table(
                    f"*Pod events for {pod.metadata.name}:*",
                    "Pod",
                    pod.metadata.name,
                    pod.metadata.namespace,
                    included_types=params.included_types,
                    max_events=params.max_events,
                )
                if events_table_block:
                    event.add_enrichment([events_table_block], {SlackAnnotations.ATTACHMENT: True})
    else:
        pods = list_pods_using_selector(dep.metadata.namespace, dep.spec.selector, "status.phase=Running")
        event.add_enrichment([MarkdownBlock(f"*Replicas: Desired ({dep.spec.replicas}) --> Running ({len(pods)})*")])
        events_table_block = get_resource_events_table(
            "*Deployment events:*",
            dep.kind,
            dep.metadata.name,
            dep.metadata.namespace,
            included_types=params.included_types,
            max_events=params.max_events,
        )
        if events_table_block:
            event.add_enrichment([events_table_block], {SlackAnnotations.ATTACHMENT: True})


@action
def external_video_enricher(event: ExecutionBaseEvent, params: VideoEnricherParams):
    """
    Attaches a video links to the finding
    """
    event.add_video_link(VideoLink(url=params.url, name=params.name))


@action
def resource_events_diff(event: KubernetesAnyChangeEvent):
    new_resource = event.obj

    if isinstance(new_resource, (Deployment, DaemonSet, StatefulSet, ReplicaSet, Pod)) \
            and not event.obj.metadata.ownerReferences:
        if isinstance(new_resource, Pod) and is_pod_finished(new_resource):
            return
        if isinstance(new_resource, ReplicaSet) and not new_resource.spec.replicas:
            return

        containers = extract_containers_k8(new_resource)
        volumes = extract_volumes_k8(new_resource)
        meta = new_resource.metadata
        container_info = [ContainerInfo.get_container_info_k8(container) for container in
                          containers] if containers else []
        volumes_info = [VolumeInfo.get_volume_info(volume) for volume in volumes] if volumes else []
        config = ServiceConfig(labels=meta.labels or {}, containers=container_info,
                               volumes=volumes_info)
        ready_pods = extract_total_pods(new_resource)
        total_pods = extract_ready_pods(new_resource)

        is_helm_release = is_release_managed_by_helm(annotations=new_resource.metadata.annotations,
                                                     labels=new_resource.metadata.labels)
        resource_version = int(meta.resourceVersion) if meta.resourceVersion else 0

        new_service = ServiceInfo(
            resource_version=resource_version,
            name=meta.name,
            namespace=meta.namespace,
            service_type=new_resource.kind,
            service_config=config,
            ready_pods=ready_pods,
            total_pods=total_pods,
            is_helm_release=is_helm_release
        )

        all_sinks = event.get_all_sinks()
        for sink_name in event.named_sinks:
            if all_sinks and all_sinks.get(sink_name, None):
                all_sinks.get(sink_name).handle_service_diff(new_service, operation=event.operation)
