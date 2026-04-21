# CLAUDE.md

프로젝트 구조, 아키텍처, 코드 스타일은 [AGENTS.md](./AGENTS.md)를 참조. 전체 모노레포 컨텍스트는 [루트 CLAUDE.md](../../CLAUDE.md)를 참조.

## Dev Commands

```bash
uv run uvicorn test_service.main:app --reload --host 0.0.0.0 --port 8000  # 개발 서버
uv run ruff check src/ tests/                                              # 린트
uv run ruff format src/ tests/                                             # 포맷
uv run pytest                                                              # 테스트
docker compose up -d                                                       # 로컬 DB
```

## Key Rules

- 모든 Python 실행은 `uv run` 사용 (`source .venv/bin/activate` 금지)
- Python 모듈명은 `test_service` (디렉토리명과 다름에 주의)
- ruff lint + format이 pre-commit hook으로 등록되어 있음
- FastAPI controller는 class-based (`self.router` 패턴)
- DI는 lazy `@property` 패턴 (Container ABC → AppContainer)
- Port 인터페이스는 ABC로 정의 (`ports/interfaces/`)
- DTO는 dataclass (`ports/dto/`)
