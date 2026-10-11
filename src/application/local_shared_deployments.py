"""Local ZIP admission and asynchronous private shared-DB preparation."""
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from adapters.aws.ecs import AwsSettings
from adapters.aws.shared_application import LocalSharedApplication, registration_digest
from application.deployment_core import source_digest
from application.infrastructure import preflight_sqlite_conversion
from adapters.database.migrations import collect_sql_migrations
from engine.application_ir import application_ir
from engine.architecture_decision import architecture_decision
from engine.compilation import compile_decision
from engine.compatibility import explicit_infrastructure_plan, infrastructure_compatibility, validate_infrastructure
from engine.deployment_policy import deployment_policy


class LocalSharedDeploymentsMixin:
    def create_shared_job(self, job_id, project, application_id, profile, owner, public,
                          requested_target='auto', public_url_required=False):
        if self.hosted or not self.local_shared_registration:
            raise ValueError('Local workload pool is not registered')
        if not public or requested_target not in {'auto', 'aws-ecs-express'}:
            raise ValueError('Shared RDS requires an explicitly permitted public AWS deployment')
        settings = self.aws_settings
        if settings.unavailable_reason():
            raise ValueError(settings.unavailable_reason())
        conversion, prepared = (preflight_sqlite_conversion(project, profile)
                                if profile.storage == 'sqlite' else (None, profile))
        if conversion is None:
            collect_sql_migrations(project)
        validate_infrastructure(prepared, 'aws-ecs-express', postgres=True)
        # Validate registration before accepting any external work.
        LocalSharedApplication(self.local_shared_registration, settings, lambda *_: None, lambda **_: None)
        digest = source_digest(project)
        policy = deployment_policy(requested_target, public, allow_data_migration=conversion is not None,
                                   public_url_required=public_url_required)
        with self.lock:
            self.ensure_application_available(application_id, 'aws-ecs-express')
            active = [x for x in self.jobs.values() if x.get('application_id') == application_id
                      and x.get('target') == 'aws-ecs-express' and x.get('result')
                      and x.get('deployment_state', 'active') == 'active']
            previous = max(active, key=lambda x: x.get('created_at', '')) if active else None
            if previous and (not previous.get('shared_postgres')
                             or previous.get('organization_id') != owner.organization_id):
                raise ValueError('Active deployment uses a different database or owner')
            self.jobs[job_id] = {'id': job_id, 'mode': 'agent', 'target': 'aws-ecs-express',
                'requested_target': requested_target, 'application_id': application_id, **owner.record(),
                'public': True, 'status': 'running', 'created_at': datetime.now(timezone.utc).isoformat(),
                'project': str(project), 'source_digest': digest, 'plan': None, 'diff': '',
                'changes': [], 'steps': 0, 'attempts': 0, 'events': [], 'aws': asdict(settings),
                'infrastructure_profile': profile.as_dict(), 'application_ir': application_ir(profile, digest).as_dict(),
                'deployment_policy': policy.as_dict(), 'shared_preparation': {'status': 'accepted'},
                'shared_registration_digest': registration_digest(self.local_shared_registration),
                'sqlite_conversion': conversion, 'shared_prepared_profile': prepared.as_dict()}
            if previous:
                self.jobs[job_id]['prior_result'] = previous['result']
                self.jobs[job_id]['replaces_job_id'] = previous['id']
            self.save(job_id)

    def run_shared_then_agent(self, job_id):
        job = self.jobs[job_id]
        def checkpoint(**fields):
            with self.lock:
                self.jobs[job_id].update(fields)
                self.save(job_id)
        try:
            if (source_digest(Path(job['project'])) != job['source_digest']
                    or registration_digest(self.local_shared_registration) != job['shared_registration_digest']
                    or job['shared_preparation']['status'] != 'accepted'):
                raise ValueError('Source or pool registration changed, or allocation needs reconciliation')
            settings = AwsSettings(**job['aws'])
            if self.cancel_requested(job_id):
                checkpoint(status='cancelled')
                return
            bridge = LocalSharedApplication(self.local_shared_registration, settings,
                lambda stage, message: self.event(job_id, stage, message), checkpoint)
            request = bridge.prepare(job['application_id'], job['organization_id'], job_id)
            if job.get('prior_result') and job['prior_result'].get('database') != request.inspect(bridge.adapter):
                raise ValueError('Active app database binding changed')
            if self.cancel_requested(job_id):
                checkpoint(status='cancelled')
                return
            if source_digest(Path(job['project'])) != job['source_digest']:
                raise ValueError('Source changed during database preparation; allocation preserved')
            from engine.compatibility import InfrastructureProfile
            from engine.deployment_policy import policy_from_record
            prepared = InfrastructureProfile(**job['shared_prepared_profile'])
            # JSON persistence turns tuples into lists; normalize the engine contract.
            prepared = InfrastructureProfile(prepared.storage, tuple(prepared.evidence), prepared.scanned_files,
                tuple(prepared.requirements), tuple(prepared.database_engines), prepared.final_image_platform,
                tuple((key, tuple(items)) for key, items in prepared.requirement_evidence),
                tuple((key, tuple(items)) for key, items in prepared.source_signals))
            plan = explicit_infrastructure_plan('aws-ecs-express', prepared, existing_postgres_id=request.database_id)
            plan.update(planner='registered-shared-pool',
                rationale='앱 전용 DB·사용자·Secret을 기존 공유 RDS에 할당하고 소유권과 TLS를 확인했습니다. 별도 RDS는 생성하지 않습니다.')
            plan['database']['workload_binding'] = asdict(request.allocation().binding())
            plan['compatibility'] = infrastructure_compatibility(prepared, 'aws-ecs-express', postgres=True, public_access=True)
            if job.get('sqlite_conversion'):
                plan['conversion_pending'] = 'sqlite-to-postgresql'
            policy = policy_from_record(job['deployment_policy'])
            decision = architecture_decision(job['application_ir'], policy, plan).as_dict()
            compiled = compile_decision(decision, job['application_ir'], policy, plan)
            checkpoint(shared_postgres=asdict(request), infrastructure_plan=plan,
                architecture_decision=decision, compilation=compiled,
                workload_database_binding=asdict(request.allocation().binding()))
            self.event(job_id, 'database_ready', '앱 전용 DB 준비 완료. 소스 변환·스키마 적용·이미지 배포를 시작합니다.')
            self.run_agent(job_id)
        except Exception as error:
            # Never expose cloud/SQL diagnostics or silently re-run uncertain effects.
            checkpoint(status='failed', shared_preparation_error_type=type(error).__name__)
            self.event(job_id, 'error', '공유 DB 배포 준비에 실패했습니다. 할당 기록과 DB는 보존했습니다. 자동으로 재시도하지 않습니다.')

    def verify_shared_result(self, job_id, result, settings, checkpoint):
        from adapters.aws.ecs import AwsExpressAdapter
        from adapters.aws.shared_application import verify_application_database
        from application.state_recovery import postgres_request_from_job

        job = self.jobs[job_id]
        self.event(job_id, 'database_verifying', '앱 전용 DB 계정으로 TLS 접속과 임시 데이터 읽기·쓰기를 검증합니다.')
        adapter = AwsExpressAdapter(lambda s, m: self.event(job_id, s, m), settings, checkpoint=checkpoint)
        try:
            verified = verify_application_database(postgres_request_from_job(job), adapter,
                                                    job_id + '-a' + str(job['attempts']))
        except Exception:
            checkpoint(result=result, deployment_state='needs_attention')
            raise
        result['database_verification'] = verified
        checkpoint(database_verification=verified)
        return result

    def run_shared_result_verification(self, job_id):
        """Upgrade an already HTTP-verified local release without rebuilding it."""
        def checkpoint(**fields):
            with self.lock:
                self.jobs[job_id].update(fields)
                self.save(job_id)
        job = self.jobs[job_id]
        try:
            from application.health import check_deployment
            verification_retry = (job.get('status') == 'failed' and bool(job.get('shared_verification_error_type'))
                                  and bool(job.get('result')) and job.get('deployment_state') == 'needs_attention')
            if (self.hosted or job.get('shared_postgres') is None
                    or (job.get('status') != 'succeeded' and not verification_retry)
                    or (job.get('deployment_state', 'active') != 'active' and not verification_retry)
                    or source_digest(Path(job['project'])) != job['source_digest']):
                raise ValueError('Only an unchanged, owned shared release can be verified')
            if not check_deployment({**job, 'status': 'succeeded', 'deployment_state': 'active'})['healthy']:
                raise ValueError('Application is no longer HTTP healthy')
            checkpoint(status='running')
            result = self.verify_shared_result(job_id, dict(job['result']), AwsSettings(**job['aws']), checkpoint)
            checkpoint(status='succeeded', result=result, deployment_state='active')
            self.event(job_id, 'succeeded', '공개 HTTP와 앱 전용 DB 읽기·쓰기 확인 완료')
        except Exception as error:
            checkpoint(status='failed', shared_verification_error_type=type(error).__name__, deployment_state='needs_attention')
            self.event(job_id, 'error', '앱은 보존했습니다. DB 검증 결과를 확인해야 합니다.')
