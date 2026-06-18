from __future__ import annotations

import argparse
import json
import traceback
from pathlib import Path

from . import downloader


def _write_result(result_path: Path, payload: dict) -> None:
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one JMComic download job.")
    parser.add_argument("--album-id", required=True)
    parser.add_argument("--option-path", required=True)
    parser.add_argument("--job-dir", required=True)
    parser.add_argument("--result-path", required=True)
    args = parser.parse_args()

    job_dir = Path(args.job_dir).resolve()
    result_path = Path(args.result_path).resolve()
    error_log_path = job_dir / "worker-error.log"

    try:
        pdf_path = downloader.download_album_pdf(
            args.album_id,
            Path(args.option_path),
            job_dir,
        )
    except downloader.DownloaderError as exc:
        error_log_path.write_text(
            downloader._redact_sensitive_log(traceback.format_exc()),
            encoding="utf-8",
        )
        _write_result(
            result_path,
            {
                "ok": False,
                "error_type": exc.__class__.__name__,
                "user_message": exc.user_message,
            },
        )
        return 2
    except Exception:
        error_log_path.write_text(
            downloader._redact_sensitive_log(traceback.format_exc()),
            encoding="utf-8",
        )
        _write_result(
            result_path,
            {
                "ok": False,
                "error_type": "UnexpectedError",
                "user_message": "下载或转换失败，请查看服务日志",
            },
        )
        return 1

    _write_result(
        result_path,
        {
            "ok": True,
            "pdf_path": str(pdf_path),
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

