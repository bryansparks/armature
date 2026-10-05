"""Cloud transport for missions — the engine side of the design §5 seam.

This subpackage lives behind the `cloud` extra (``pip install
armature-agents[cloud]``). The default engine install never imports it:
boto3 exists only here. Modules are imported directly
(``from armature.transport.s3work import S3WorkStore``) — no re-exports,
so a missing extra surfaces at the exact import that needs it.

Contents (spec 2026-10-05):
    naming    job-id minting + name validation (hostile-id gate)
    s3io      S3 helpers + the jobs/ results-layout readers
    s3work    S3WorkStore — the S3 implementation of the WorkStore protocol
    settle    runner steps 4.7 inject / 7.6 settle (work-unit resting states)
    workops   submit gates + launch bookkeeping (start_work_unit)
    sweep     the flock sweep: repair, submit, notify, hold
"""