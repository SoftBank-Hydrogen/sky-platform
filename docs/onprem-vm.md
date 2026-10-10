# Linux VM 배포 대상

Sky의 `onprem-vm` 대상은 Sky와 다른 Linux VM의 Docker Engine에 SSH로 연결해 Compose 서비스를 실행한다. 자동 선택 대상은 아니며, 사용자가 직접 선택해야 한다. 현재는 상태 없는 HTTP 앱 한 개를 컨테이너 하나로 실행한다. SQLite 볼륨, PostgreSQL 연결, HTTPS 프록시, VM 생성은 이 대상에서 지원하지 않는다.

Sky 서버를 시작하기 전에 다음 값을 **서버 환경변수**로 설정한다. SSH 키는 저장소나 업로드 앱에 넣지 않는다.

```sh
export SKY_VM_SSH_HOST=vm.example.com
export SKY_VM_SSH_USER=sky
export SKY_VM_PUBLIC_HOST=vm.example.com
```

VM에는 Docker Engine과 Compose 플러그인, Sky 호스트에는 Docker CLI·Compose·SSH 클라이언트가 필요하다. SSH 계정은 VM의 Docker 소켓에 접근할 수 있어야 한다. 이 권한은 사실상 VM 관리자 권한이므로 Sky 전용 VM과 계정을 사용한다. SSH 호스트 키는 별도 경로로 지문을 확인한 뒤 `known_hosts`에 등록한다. 초기 연결 확인은 `docker --host ssh://sky@vm.example.com info`로 할 수 있다.

배포 시 Docker가 VM의 사용 가능한 호스트 포트를 배정한다. Sky는 `SKY_VM_PUBLIC_HOST:<배정 포트>`에서 HTTP 200을 직접 확인한 뒤에만 성공 처리한다. VM 방화벽과 라우팅이 이 포트를 Sky 서버와 접속 사용자에게 열어 주어야 한다. VM이 사설망에 있으면 이 URL은 해당 네트워크 사용자에게만 접근 가능하다. **HTTPS와 인터넷 공개 주소는 제공하지 않는다.** 심사위원 접속에 쓰려면 별도 고정 진입점·TLS 구성이 필요하다.

배포 기록에는 접속 VM과 공개 호스트를 남긴다. 종료 때 기록된 VM·Compose 프로젝트·컨테이너·이미지 소유권을 확인하고, Sky가 만든 컨테이너와 이미지 태그만 삭제한다. 실패한 시도도 기록된 시도 ID로 정리한다. 실제 원격 VM 배포·갱신·종료 검증은 VM 연결 후 수행해야 한다.
