"""
Claude Code hook: 파일을 고친 뒤 시험을 자동으로 돌린다 (PostToolUse, Edit/Write).

- .py 파일, docs/*.md, 설정 파일(*.yaml)을 고쳤을 때만 돈다.
- 저장소의 .venv에 pytest가 있으면 그걸 쓰고, 없으면 이 스크립트를 실행한 Python을 쓴다.
- pytest가 아예 없으면 막지 않고 안내만 한다 (설치 전에도 작업은 되게).
- 시험이 실패하면 exit 2 + 실패 내용을 stderr로 보낸다 → Claude가 보고 고친다.
"""

import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
WATCH_SUFFIX = {".py", ".yaml", ".yml"}


def should_run(path_text: str) -> bool:
    if not path_text:
        return False
    path = pathlib.Path(path_text).resolve()
    try:
        rel = path.relative_to(ROOT)
    except ValueError:
        return False          # 저장소 밖 파일
    if rel.parts and rel.parts[0] in (".venv", "data", ".git"):
        return False
    if path.suffix in WATCH_SUFFIX:
        return True
    return rel.parts[:1] == ("docs",) and path.suffix == ".md"


def find_python() -> str:
    for cand in (ROOT / ".venv" / "Scripts" / "python.exe", ROOT / ".venv" / "bin" / "python"):
        if cand.exists():
            return str(cand)
    return sys.executable


def main() -> int:
    for stream in (sys.stdout, sys.stderr):      # Windows 기본 인코딩(cp949) 대신 UTF-8로 내보냄
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    tool_input = payload.get("tool_input") or {}
    response = payload.get("tool_response") or {}
    path_text = tool_input.get("file_path") or response.get("filePath") or ""
    if not should_run(path_text):
        return 0

    python = find_python()
    has_pytest = subprocess.run([python, "-c", "import pytest"], cwd=ROOT,
                                capture_output=True).returncode == 0
    if not has_pytest:
        print("[시험 hook] pytest가 설치되어 있지 않아 시험을 건너뜀: "
              "pip install -r laptop/requirements.txt", file=sys.stderr)
        return 0

    try:
        result = subprocess.run([python, "-m", "pytest", "-q", "-x", "--no-header", "-p", "no:cacheprovider"],
                                cwd=ROOT, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=110)
    except subprocess.TimeoutExpired:
        print("[시험 hook] 시험이 110초 안에 끝나지 않음", file=sys.stderr)
        return 2
    if result.returncode == 0:
        return 0
    if result.returncode == 5:     # 모은 시험이 없음
        return 0
    out = (result.stdout + result.stderr).strip().splitlines()
    print("[시험 hook] python -m pytest 실패. 고친 내용 때문인지 확인하세요.\n"
          + "\n".join(out[-60:]), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
