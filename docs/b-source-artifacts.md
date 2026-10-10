# B source artifacts

This adds the application source storage path for B admission/builds: normalized original source and prepared execution snapshots in the existing private artifacts bucket. It does **not** enable a public upload endpoint or change the current local upload/deployment behavior. API admission must later persist these references with jobs/operations atomically; the read-only B API remains read-only. PR #16's runtime/image preparation is independent and not included in this PR.

## Storage contract

Configure SKY_ARTIFACTS_BUCKET, SKY_AWS_REGION and SKY_AWS_ACCOUNT_ID. Compose `SourceArtifactService(S3SourceArtifactStore(S3ArtifactSettings.from_environment()))` at the later admission/worker boundary. boto3 is available through the existing state-postgres extra; the prepared service image in PR #16 installs it.

`capture_upload(verified_principal, application_id, upload_id, zip_bytes)` validates the upload before S3 writes and returns CapturedSource with a SourceArtifact and raw upload SHA-256. upload_id is a stable, caller-generated 32-character lowercase hexadecimal identity. Supply the same ID and input on retry; S3 upload itself is not a complete API idempotency contract.

The original is **normalized source before Sky transformations**, not the raw ZIP bytes. Existing ZIP extraction removes .env/.env.*, .git, node_modules and other ignored source paths; these excluded files are not persisted. This is a known-file filter, not a guarantee that code contains no embedded secrets. A single top-level application folder is normalized to the project root. ZIP timestamps and file modes are canonicalized, preserving the existing extraction semantics; executable modes/empty directories are not part of source_digest.

A reference has version=1, organization_id, application_id, upload_id, kind, sha256, size and source_digest. It contains no bucket, URL or arbitrary object key. The key is derived:

`sources/{organization_id}/{application_id}/{upload_id}/{original|prepared}/{sha256}.zip`

Store the source artifact record in trusted DB metadata/commands. The upload-byte hash, snapshot-byte hash and logical source_digest have different meanings; keep all required evidence. Do not put local temporary paths into durable job commands.

`capture_prepared(verified_principal, original_ref, project, expected_digest=approved_digest)` binds the prepared copy to the original's organization/application/upload identity and requires an approved logical source digest. It rejects excluded files, links, special files or changed source. Existing original objects are never changed. A snapshot is a deterministic stored ZIP, bounded to 128 MiB including path/header overhead.

`with service.restore(verified_principal, reference) as project:` verifies stored bytes, validates the ZIP again, extracts into a private disposable directory and verifies source_digest before yielding the project. Success and failure both remove this directory. Copy no credentials into this source tree. Human principals must come from verified authentication/membership; worker access must later be composed from trusted DB ownership and an execution lease, never identities asserted by an SQS message.

## Limits and integrity

- Incoming ZIP at most 20 MiB; extracted file bytes at most 100 MiB.
- At most 5,000 ZIP entries and 5,000 expanded file/directory paths; path depth at most 32 and UTF-8 path length at most 1,024 bytes.
- Only stored/deflated ZIP, no encrypted entries, symlinks, special files, traversal, duplicate/conflicting paths or ambiguous application root.
- Prepared snapshots use equivalent bounded traversal and source consistency checks. Capture/restoration allocates temporary disk; storage/download uses bounded in-memory byte buffers. Concurrent admission needs an aggregate memory/disk budget before HTTP activation.
- Source access requires same-organization DEPLOY permission, including for foreign administrators. Application ownership/admission validation remains the caller's responsibility; the storage adapter is an internal trusted boundary.

PutObject uses expected bucket owner, AES256 encryption, SHA-256 checksum and If-None-Match=*; no public ACL or presigned URL is created. A duplicate conditional request is accepted only after reading and verifying identical size/hash/metadata. Timeouts/conflicts are uncertain outcomes and are not blindly retried. A caller retry with the stable identity can safely verify the existing object. SDK connection/read timeouts are bounded and automatic request attempts are limited to one.

Downloads require matching owner-derived key, size, metadata, encryption and SHA-256. Reads request at most the declared size plus one byte and always close the response. Missing data is FileNotFoundError, service/IAM failures are sanitized OSError, and corrupt content is ValueError. Bodies and credentials are never printed.

## Infrastructure and cleanup

This follows sky-infra's sources/ prefix, private bucket, AES256 encryption and versioning. Current common task policy permits GetObject/PutObject; sky-builder reads sources/*. No GetObjectVersion permission or version-pinned reads are introduced: current content is hash-verified, and a changed/deleted current object fails rather than falling back to unverified data. Conditional creation is not S3 Object Lock; external writers/deletes must still be controlled through IAM/bucket policy.

No bucket/list/delete/migration operation runs here. Upload succeeds before later DB admission; a DB failure may leave an unreferenced immutable object. Do not automatically delete after an ambiguous outcome. Coordinate garbage collection with DB references and in-flight builds. Existing sources/ lifecycle retention can expire referenced snapshots and prevent rebuild/rollback; align retention with product requirements before production. It does not affect already running images.

## Verification

Tests cover upload -> normalized original -> prepared copy -> restored source, hash/ownership failures, ZIP/path/expansion limits, excluded files, deterministic references and temporary cleanup. S3 responses use mocked clients and actual boto3 Stubber request validation; no live AWS writes or operational DB changes are performed. Existing upload behavior remains separately covered by its regression tests.

[AWS conditional writes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html) and [PutObject API](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutObject.html) describe the request and uncertain/conflicting outcomes used here.
