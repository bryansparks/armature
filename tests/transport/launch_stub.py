"""Record-only launch callable for the sweep/main tests — the real launcher
(dispatch.ops.run_workflow, ECS RunTask) is deployment mechanism and its
integration tests live in armature-dispatch.

Models the launcher contract the sweep relies on: accepts the client kwargs
main() binds (s3/ecs/ec2), and writes the jobs/<id>/task.json the real
launcher leaves behind — without it the repair pass would read a just
launched job as an orphan. The task arn is fabricated; tests that exercise
task-state logic stage their own task.json over it."""
import json

import boto3

CALLS: list[dict] = []


def reset() -> None:
    CALLS.clear()


def launch(bucket, workflow, inputs, *, job_id, work_unit, **_):
    if workflow == "not-registered":
        raise LookupError(f"no task def family dispatch-runner-{workflow}")
    boto3.client("s3").put_object(
        Bucket=bucket, Key=f"jobs/{job_id}/task.json",
        Body=json.dumps({"job_id": job_id, "package_name": workflow,
                         "package_version": "1.0", "inputs": inputs,
                         "submitted_at": "now",
                         "task_arn": f"arn:aws:ecs:us-east-1:123456789012:"
                                     f"task/dispatch/stub-{job_id}"}).encode())
    CALLS.append({"bucket": bucket, "workflow": workflow, "inputs": inputs,
                  "job_id": job_id, "work_unit": work_unit})