"""One fenced AWS submission; readiness and the job projection must both commit."""


def deploy_built_image(consumer, lease, request, checkpoint, guard, stop, delivery):
    from application.build_consumer import OwnershipLost
    intent = consumer.deployer.prepare(request, checkpoint["build_result"])
    guard()
    checkpoint.update(stage="deploying", deployment_intent=intent)
    if not consumer.store.build_progress(lease, checkpoint, stage="deploying"):
        raise OwnershipLost()
    if not consumer.store.begin_external(lease, intent):
        raise OwnershipLost()
    guard()
    consumer.deployer.create(intent)
    until = consumer.clock() + consumer.deployment_timeout_seconds
    while not stop.is_set() and consumer.clock() < until:
        guard()
        try:
            result = consumer.deployer.observe(intent)
        except OSError:
            result = None
        if result is not None:
            guard()
            checkpoint.update(stage="deployed", deployment_result=result)
            if not consumer.store.observe_external(lease, result, checkpoint):
                raise OwnershipLost()
            if not consumer.store.deployment_complete(lease, checkpoint, result):
                raise OwnershipLost()
            consumer.queue.delete(delivery)
            return "succeeded"
        stop.wait(consumer.poll_seconds)
    # Preserve the AWS intent and application lock even on SIGTERM or a timeout.
    if consumer.store.build_progress(lease, {**checkpoint, "reason": "deployment_observation_incomplete"}, stage="needs_attention"):
        consumer.queue.delete(delivery)
    return "needs_attention"
