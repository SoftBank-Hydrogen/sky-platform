"""Explicit binding to an existing, application-labelled Cloud SQL database."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class CloudSqlRequest:
    application_id: str
    project: str
    region: str
    instance: str
    database: str
    user: str
    password_secret: str

    def validate(self):
        patterns = {
            'application_id': r'[a-z][a-z0-9-]{2,30}',
            'project': r'[a-z][a-z0-9-]{4,28}[a-z0-9]',
            'region': r'[a-z]+-[a-z]+[0-9]+',
            'instance': r'[a-z][a-z0-9-]{0,96}[a-z0-9]',
            'database': r'[a-z][a-z0-9_]{0,62}',
            'user': r'[a-z][a-z0-9_]{0,62}',
            'password_secret': r'[A-Za-z0-9_-]{1,255}:[1-9][0-9]*',
        }
        for name, pattern in patterns.items():
            if not isinstance(getattr(self, name), str) or not re.fullmatch(pattern, getattr(self, name)):
                raise ValueError('Invalid Cloud SQL binding: ' + name)
        if self.database in {'postgres', 'template0', 'template1'} or self.user == 'postgres':
            raise ValueError('Cloud SQL에는 앱 전용 DB와 사용자를 지정하세요.')
        if len('/cloudsql/' + self.connection_name + '/.s.PGSQL.5432') >= 108:
            raise ValueError('Cloud SQL 연결 이름이 Unix 소켓 경로 길이 제한을 초과합니다.')

    @property
    def connection_name(self):
        return f'{self.project}:{self.region}:{self.instance}'

    @property
    def database_id(self):
        return f'{self.connection_name}/{self.database}'

    def environment(self):
        self.validate()
        # The managed Cloud SQL connector encrypts the remote connection. The app talks to its Unix socket.
        return {'PGHOST': '/cloudsql/' + self.connection_name, 'PGPORT': '5432',
                'PGDATABASE': self.database, 'PGUSER': self.user, 'PGSSLMODE': 'disable'}

    def flags(self):
        self.validate()
        return ['--set-cloudsql-instances', self.connection_name,
                '--set-secrets', 'PGPASSWORD=' + self.password_secret]

    def inspect(self, gcloud):
        """Read metadata only. Never fetch the password into Sky or the model context."""
        self.validate()
        instance = json.loads(gcloud(['sql', 'instances', 'describe', self.instance, '--format=json'], private=True))
        if (instance.get('connectionName') != self.connection_name
                or instance.get('region') != self.region
                or not instance.get('databaseVersion', '').startswith('POSTGRES_')
                or instance.get('state') != 'RUNNABLE'
                or instance.get('settings', {}).get('userLabels', {}).get('sky-app') != self.application_id):
            raise ValueError('Cloud SQL 프로젝트·리전·PostgreSQL 상태 또는 sky-app 소유 라벨이 다릅니다.')
        if not any(ip.get('type') == 'PRIMARY' for ip in instance.get('ipAddresses', [])):
            raise ValueError('현재 Cloud SQL 연결은 공개 IP의 관리형 커넥터 경로만 지원합니다. 사설 VPC 연결은 별도 구현이 필요합니다.')
        database = json.loads(gcloud(['sql', 'databases', 'describe', self.database,
                                     '--instance', self.instance, '--format=json'], private=True))
        if database.get('name') != self.database or database.get('instance') != self.instance:
            raise ValueError('Cloud SQL 데이터베이스가 요청과 다릅니다.')
        users = json.loads(gcloud(['sql', 'users', 'list', '--instance', self.instance, '--format=json'], private=True))
        if not any(user.get('name') == self.user and user.get('type', 'BUILT_IN') == 'BUILT_IN' for user in users):
            raise ValueError('Cloud SQL 앱 사용자가 없습니다.')
        secret, version = self.password_secret.split(':')
        metadata = json.loads(gcloud(['secrets', 'versions', 'describe', version, '--secret', secret,
                                     '--format=json'], private=True))
        if metadata.get('state') != 'ENABLED':
            raise ValueError('활성 Secret Manager 비밀번호 버전이 필요합니다.')
        return {'provider': 'gcp', 'database_id': self.database_id,
                'connection_name': self.connection_name, 'database': self.database, 'user': self.user}
