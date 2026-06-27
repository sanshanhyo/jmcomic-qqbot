from __future__ import annotations

import logging
import os
import re
import shutil
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ALBUM_ID_RE = re.compile(r"^\d{1,12}$")
ILLEGAL_FILENAME_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
COOKIE_LOG_RE = re.compile(
    r"(?i)(Cookie['\"]?\s*:\s*['\"]?)([^'\"\]}]+)|"
    r"(cookies['\"]?\s*:\s*['\"]?)([^'\"\]}]+)|"
    r"(AVS['\"]?\s*:\s*['\"]?)([^'\"\]}]+)"
)
MAX_FILENAME_BYTES = 180
DEFAULT_MAX_IMAGE_THREADS = 16
DEFAULT_MAX_PHOTO_THREADS = 4


class DownloaderError(Exception):
    """Base error with a message that is safe to show to QQ users."""

    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


class AlbumNotFoundError(DownloaderError):
    pass


class PdfGenerationError(DownloaderError):
    pass


class DownloadError(DownloaderError):
    pass


class PreviewError(DownloaderError):
    pass


class SearchError(DownloaderError):
    pass


def _truncate_utf8(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _truncate_filename_bytes(name: str, fallback: str, max_bytes: int) -> str:
    suffix = Path(name).suffix
    stem = Path(name).stem if suffix else name
    suffix_bytes = len(suffix.encode("utf-8"))
    stem_budget = max(1, max_bytes - suffix_bytes)
    stem = _truncate_utf8(stem, stem_budget).strip(" .")
    if stem:
        return f"{stem}{suffix}"

    fallback = _truncate_utf8(fallback, max_bytes).strip(" .")
    return fallback or "output.pdf"


def sanitize_filename(
    name: str,
    fallback: str = "output.pdf",
    max_length: int = 180,
    max_bytes: int = MAX_FILENAME_BYTES,
) -> str:
    cleaned = ILLEGAL_FILENAME_CHARS_RE.sub("_", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned:
        cleaned = fallback
    if len(cleaned) > max_length:
        stem = Path(cleaned).stem[: max_length - 4].strip(" .")
        suffix = Path(cleaned).suffix or ".pdf"
        cleaned = f"{stem}{suffix}"
    if len(cleaned.encode("utf-8")) > max_bytes:
        cleaned = _truncate_filename_bytes(cleaned, fallback, max_bytes)
    return cleaned


def sanitize_title(title: str | None, fallback: str = "album", max_length: int = 120) -> str:
    cleaned = ILLEGAL_FILENAME_CHARS_RE.sub("_", title or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if cleaned.lower().endswith(".pdf"):
        cleaned = cleaned[:-4].strip(" .")
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].strip(" .")
    return cleaned or fallback


def _album_title_from_detail(album_id: str, album: object | None) -> str:
    if album is None:
        return f"JM{album_id}"
    title = getattr(album, "title", None) or getattr(album, "name", None)
    return str(title or f"JM{album_id}")


def _pdf_filename(album_id: str, title: str | None = None) -> str:
    prefix = f"[JM{album_id}]"
    if title is None or not sanitize_title(title, fallback=""):
        return sanitize_filename(f"{prefix}.pdf", fallback=f"{prefix}.pdf")
    safe_title = sanitize_title(title, fallback="album")
    return sanitize_filename(f"{prefix}{safe_title}.pdf", fallback=f"{prefix}.pdf")


def _positive_int_or_none(value: object) -> int | None:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _preview_page_count_stop_after() -> int:
    return (_env_positive_int("LARGE_ALBUM_WARNING_PAGES") or 100) + 1


def _episode_ids(album: object) -> list[str]:
    episode_list = getattr(album, "episode_list", None)
    ids: list[str] = []
    if isinstance(episode_list, list):
        for episode in episode_list:
            if isinstance(episode, (tuple, list)) and episode:
                ids.append(str(episode[0]))

    if ids:
        return ids

    album_id = getattr(album, "album_id", None) or getattr(album, "id", None)
    return [str(album_id)] if album_id else []


def _photo_page_count(photo: object) -> int | None:
    page_arr = getattr(photo, "page_arr", None)
    if isinstance(page_arr, list):
        count = len(page_arr)
        return count if count > 0 else None

    try:
        count = len(photo)  # type: ignore[arg-type]
    except (TypeError, AttributeError):
        return None
    return count if count > 0 else None


def _resolve_preview_page_count(
    client: object,
    album: object,
    stop_after: int | None = None,
) -> tuple[int | None, bool]:
    page_count = _positive_int_or_none(getattr(album, "page_count", None))
    if page_count is not None:
        return page_count, False

    total = 0
    found_any = False
    for photo_id in _episode_ids(album):
        try:
            photo = client.get_photo_detail(photo_id, fetch_album=False)
        except Exception:
            logger.debug("Could not fetch photo detail for preview page count: %s", photo_id, exc_info=True)
            continue
        count = _photo_page_count(photo)
        if count is None:
            continue
        found_any = True
        total += count
        if stop_after is not None and total >= stop_after:
            return total, True

    return (total if found_any else None), False


def _looks_like_missing_album(exc: Exception) -> bool:
    text = str(exc).lower()
    needles = ("404", "not found", "不存在", "无法找到", "不存在该", "album not")
    return any(needle in text for needle in needles)


def _download_error_message(exc: Exception) -> str:
    text = str(exc).lower()
    if "403" in text or "ip地区禁止访问" in text or "爬虫被识别" in text:
        return "JM 请求被拒绝：IP 地区禁止访问或被识别为爬虫，请检查网络代理或 Cookie"
    if "tls connect error" in text or "openssl" in text:
        return "JM 网络连接失败：TLS 握手失败，请检查网络或代理"
    if "timeout" in text or "timed out" in text:
        return "JM 网络连接超时，请稍后重试"
    return "下载失败，请稍后重试"


def _redact_sensitive_log(text: str) -> str:
    return COOKIE_LOG_RE.sub(lambda m: f"{m.group(1) or m.group(3) or m.group(5)}<redacted>", text)


def _log_captured_jmcomic_output(stdout: StringIO, stderr: StringIO) -> None:
    captured = "\n".join(part for part in (stdout.getvalue(), stderr.getvalue()) if part.strip())
    if captured:
        logger.debug("JMComic output:\n%s", _redact_sensitive_log(captured))


def _ensure_child_path(child: Path, parent: Path) -> Path:
    resolved_child = child.resolve()
    resolved_parent = parent.resolve()
    if not resolved_child.is_relative_to(resolved_parent):
        raise PdfGenerationError("PDF 生成失败：输出路径异常")
    return resolved_child


def _natural_sort_key(path: Path) -> list[int | str]:
    parts: list[int | str] = []
    for piece in re.split(r"(\d+)", path.as_posix().lower()):
        if not piece:
            continue
        parts.append(int(piece) if piece.isdigit() else piece)
    return parts


def _collect_image_paths(images_dir: Path) -> list[Path]:
    if not images_dir.exists():
        return []
    paths = [
        path.resolve()
        for path in images_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES and path.stat().st_size > 0
    ]
    return sorted(paths, key=_natural_sort_key)


def _load_img2pdf_module() -> Any:
    try:
        import img2pdf
    except ImportError as exc:
        raise PdfGenerationError("PDF 生成失败：后端缺少 img2pdf 依赖，请联系管理员安装") from exc
    return img2pdf


def _write_images_to_pdf(album_id: str, images_dir: Path, output_dir: Path, title: str | None = None) -> Path:
    image_paths = _collect_image_paths(images_dir)
    if not image_paths:
        raise PdfGenerationError("PDF 生成失败：未找到可转换图片")

    img2pdf = _load_img2pdf_module()
    output_dir.mkdir(parents=True, exist_ok=True)
    fallback_path = output_dir / _pdf_filename(album_id, title)
    try:
        fallback_path.write_bytes(img2pdf.convert([str(path) for path in image_paths]))
    except Exception as exc:
        raise PdfGenerationError("PDF 生成失败：图片转换失败") from exc

    if not fallback_path.is_file() or fallback_path.stat().st_size <= 0:
        raise PdfGenerationError("PDF 生成失败：最终文件无效")
    return fallback_path


def _can_fallback_to_image_conversion(exc: PdfGenerationError) -> bool:
    message = str(exc)
    return "未找到输出文件" in message or "输出文件为空" in message


def _remove_existing_pdfs(output_dir: Path) -> None:
    if not output_dir.exists():
        return
    for pdf_path in output_dir.rglob("*.pdf"):
        safe_pdf_path = _ensure_child_path(pdf_path, output_dir)
        safe_pdf_path.unlink(missing_ok=True)


def _finalize_or_convert_pdf(album_id: str, output_dir: Path, images_dir: Path, title: str | None = None) -> Path:
    try:
        return _finalize_single_pdf(album_id, output_dir, preferred_title=title)
    except PdfGenerationError as exc:
        if not _can_fallback_to_image_conversion(exc):
            raise
        if not _collect_image_paths(images_dir):
            raise

        logger.warning(
            "JMComic export_pdf did not produce a usable PDF for JM%s; converting downloaded images directly.",
            album_id,
        )
        _remove_existing_pdfs(output_dir)
        _write_images_to_pdf(album_id, images_dir, output_dir, title=title)
        return _finalize_single_pdf(album_id, output_dir, preferred_title=title)


def _cleanup_downloaded_images(images_dir: Path, job_path: Path) -> None:
    if not images_dir.exists():
        return
    safe_images_dir = _ensure_child_path(images_dir, job_path)
    if safe_images_dir == job_path.resolve():
        return
    shutil.rmtree(safe_images_dir, ignore_errors=True)


def _finalize_single_pdf(album_id: str, output_dir: Path, preferred_title: str | None = None) -> Path:
    output_dir = output_dir.resolve()
    pdfs = [path for path in output_dir.rglob("*.pdf") if path.is_file()]

    if not pdfs:
        raise PdfGenerationError("PDF 生成失败：未找到输出文件")

    non_empty_pdfs = [path for path in pdfs if path.stat().st_size > 0]
    if len(non_empty_pdfs) != len(pdfs):
        raise PdfGenerationError("PDF 生成失败：输出文件为空")

    if len(non_empty_pdfs) != 1:
        raise PdfGenerationError("PDF 生成失败：输出文件数量异常")

    pdf_path = _ensure_child_path(non_empty_pdfs[0], output_dir)
    prefix = f"[JM{album_id}]"
    if preferred_title:
        current_name = _pdf_filename(album_id, preferred_title)
    else:
        current_name = sanitize_filename(pdf_path.name, fallback=f"{prefix}.pdf")
        if f"JM{album_id}" not in current_name.upper():
            title = re.sub(rf"^(?:JM)?{re.escape(album_id)}[\s_\-]*", "", pdf_path.stem, flags=re.I)
            current_name = _pdf_filename(album_id, title)

    final_name = sanitize_filename(current_name, fallback=f"{prefix}.pdf")
    if f"JM{album_id}" not in final_name.upper():
        final_name = f"{prefix}{final_name}"

    final_path = _ensure_child_path(output_dir / final_name, output_dir)
    if pdf_path != final_path:
        if final_path.exists():
            final_path.unlink()
        pdf_path.replace(final_path)

    if not final_path.exists() or final_path.stat().st_size <= 0:
        raise PdfGenerationError("PDF 生成失败：最终文件无效")

    return final_path


def _set_job_download_dir(option: object, images_dir: Path) -> None:
    try:
        option.dir_rule.base_dir = str(images_dir)
    except Exception:
        logger.warning("Could not override jmcomic dir_rule.base_dir; using option file value.")


def _env_positive_int(name: str) -> int | None:
    value = os.getenv(name)
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError:
        logger.warning("Invalid integer for %s; ignoring.", name)
        return None
    if parsed <= 0:
        logger.warning("Invalid non-positive integer for %s; ignoring.", name)
        return None
    return parsed


def _set_download_threading(option: object) -> None:
    image_threads = _env_positive_int("JM_DOWNLOAD_IMAGE_THREADS")
    photo_threads = _env_positive_int("JM_DOWNLOAD_PHOTO_THREADS")

    try:
        if image_threads is not None:
            option.download.threading.image = image_threads
        if photo_threads is not None:
            option.download.threading.photo = photo_threads
        _cap_download_threading(option)
        logger.info(
            "Using JMComic download threading: image=%s, photo=%s",
            option.download.threading.image,
            option.download.threading.photo,
        )
    except Exception:
        logger.warning("Could not override jmcomic download threading; using option file values.")


def _cap_download_threading(option: object) -> None:
    image_cap = _env_positive_int("JM_DOWNLOAD_MAX_IMAGE_THREADS") or DEFAULT_MAX_IMAGE_THREADS
    photo_cap = _env_positive_int("JM_DOWNLOAD_MAX_PHOTO_THREADS") or DEFAULT_MAX_PHOTO_THREADS

    current_image = _positive_int_or_none(getattr(option.download.threading, "image", None))
    current_photo = _positive_int_or_none(getattr(option.download.threading, "photo", None))
    if current_image is not None and current_image > image_cap:
        logger.warning("Capping JM_DOWNLOAD image threads from %s to %s.", current_image, image_cap)
        option.download.threading.image = image_cap
    if current_photo is not None and current_photo > photo_cap:
        logger.warning("Capping JM_DOWNLOAD photo threads from %s to %s.", current_photo, photo_cap)
        option.download.threading.photo = photo_cap


def download_album_pdf(album_id: str, option_path: str | Path, job_dir: str | Path) -> Path:
    if not ALBUM_ID_RE.fullmatch(album_id):
        raise DownloadError("编号格式错误：只允许 1 到 12 位数字")

    option_file = Path(option_path).expanduser().resolve()
    if not option_file.is_file():
        raise DownloadError("JMComic 配置文件不存在，请检查 JMCOMIC_OPTION_PATH")

    job_path = Path(job_dir).resolve()
    images_dir = job_path / "images"
    output_dir = job_path / "pdf"
    images_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        from jmcomic import Feature, create_option_by_file, download_album
    except ImportError as exc:
        raise DownloadError("未安装 jmcomic，请先安装项目依赖") from exc

    try:
        _load_img2pdf_module()
        option = create_option_by_file(str(option_file))
        _set_job_download_dir(option, images_dir)
        _set_download_threading(option)
        stdout = StringIO()
        stderr = StringIO()
        album_title = None
        with redirect_stdout(stdout), redirect_stderr(stderr):
            album, _downloader = download_album(
                album_id,
                option,
                extra=Feature.export_pdf(
                    pdf_dir=str(output_dir),
                    filename_rule="Aid_Atitle",
                    delete_original_file=False,
                ),
            )
            album_title = _album_title_from_detail(album_id, album)
        _log_captured_jmcomic_output(stdout, stderr)
    except DownloaderError:
        raise
    except Exception as exc:
        if "stdout" in locals() and "stderr" in locals():
            _log_captured_jmcomic_output(stdout, stderr)
        if _looks_like_missing_album(exc):
            raise AlbumNotFoundError("JM 内容不存在或不可访问") from exc
        raise DownloadError(_download_error_message(exc)) from exc

    final_path = _finalize_or_convert_pdf(album_id, output_dir, images_dir, title=album_title)
    _cleanup_downloaded_images(images_dir, job_path)
    return final_path


def estimate_download_seconds(page_count: int | None) -> int | None:
    if not page_count or page_count <= 0:
        return None
    return max(60, int(page_count * 2.5))


def format_estimated_time(seconds: int | None) -> str:
    if seconds is None:
        return "预计时间未知，取决于页数和网络"
    minutes = max(1, round(seconds / 60))
    high_minutes = max(minutes + 1, round(minutes * 1.5))
    if minutes == high_minutes:
        return f"预计约 {minutes} 分钟"
    return f"预计约 {minutes}-{high_minutes} 分钟"


def _normalize_search_query(query: str) -> str:
    normalized = re.sub(r"\s+", " ", query).strip()
    if not normalized:
        raise SearchError("搜索关键词不能为空")
    if len(normalized) > 40:
        raise SearchError("搜索关键词太长啦，请控制在 40 个字符以内")
    return normalized


def _search_page_to_result(query: str, page: int, search_page: object, limit: int) -> dict:
    results: list[dict[str, object]] = []
    content = getattr(search_page, "content", [])
    if not isinstance(content, list):
        content = []

    for item in content:
        if not isinstance(item, (tuple, list)) or len(item) < 2:
            continue
        album_id, info = item[0], item[1]
        album_id = str(album_id)
        if not ALBUM_ID_RE.fullmatch(album_id):
            continue
        info = info if isinstance(info, dict) else {}
        title = str(info.get("name") or info.get("title") or f"JM{album_id}")
        raw_tags = info.get("tags")
        tags = [str(tag) for tag in raw_tags[:8]] if isinstance(raw_tags, list) else []
        results.append({"album_id": album_id, "title": title, "tags": tags})
        if len(results) >= limit:
            break

    try:
        total = int(getattr(search_page, "total", len(results)) or 0)
    except (TypeError, ValueError):
        total = len(results)

    return {
        "query": query,
        "page": page,
        "total": max(total, len(results)),
        "results": results,
    }


def search_albums(query: str, option_path: str | Path, page: int = 1, limit: int = 5) -> dict:
    query = _normalize_search_query(query)
    page = max(1, min(int(page), 5))
    limit = max(1, min(int(limit), 10))

    option_file = Path(option_path).expanduser().resolve()
    if not option_file.is_file():
        raise SearchError("JMComic 配置文件不存在，请检查 JMCOMIC_OPTION_PATH")

    try:
        from jmcomic import create_option_by_file
    except ImportError as exc:
        raise SearchError("未安装 jmcomic，请先安装项目依赖") from exc

    stdout = StringIO()
    stderr = StringIO()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            option = create_option_by_file(str(option_file))
            client = option.new_jm_client()
            search_page = client.search_site(query, page=page)
        _log_captured_jmcomic_output(stdout, stderr)
    except DownloaderError:
        raise
    except Exception as exc:
        _log_captured_jmcomic_output(stdout, stderr)
        raise SearchError(_download_error_message(exc)) from exc

    return _search_page_to_result(query, page, search_page, limit)


def fetch_album_preview(album_id: str, option_path: str | Path) -> dict:
    if not ALBUM_ID_RE.fullmatch(album_id):
        raise PreviewError("编号格式错误：只允许 1 到 12 位数字")

    option_file = Path(option_path).expanduser().resolve()
    if not option_file.is_file():
        raise PreviewError("JMComic 配置文件不存在，请检查 JMCOMIC_OPTION_PATH")

    try:
        from jmcomic import JmcomicText, create_option_by_file
    except ImportError as exc:
        raise PreviewError("未安装 jmcomic，请先安装项目依赖") from exc

    stdout = StringIO()
    stderr = StringIO()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            option = create_option_by_file(str(option_file))
            client = option.new_jm_client()
            album = client.get_album_detail(album_id)
            page_count, page_count_is_estimated = _resolve_preview_page_count(
                client,
                album,
                stop_after=_preview_page_count_stop_after(),
            )
        _log_captured_jmcomic_output(stdout, stderr)
    except DownloaderError:
        raise
    except Exception as exc:
        _log_captured_jmcomic_output(stdout, stderr)
        if _looks_like_missing_album(exc):
            raise PreviewError("JM 内容不存在或不可访问") from exc
        raise PreviewError(_download_error_message(exc)) from exc

    estimated_seconds = estimate_download_seconds(page_count)
    return {
        "album_id": str(album_id),
        "title": str(getattr(album, "title", None) or getattr(album, "name", None) or f"JM{album_id}"),
        "cover_url": JmcomicText.get_album_cover_url(album_id),
        "page_count": page_count,
        "page_count_is_estimated": page_count_is_estimated,
        "estimated_seconds": estimated_seconds,
        "estimated_text": format_estimated_time(estimated_seconds),
    }
