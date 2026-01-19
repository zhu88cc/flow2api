"""File caching service"""
import os
import asyncio
import hashlib
import time
from pathlib import Path
from typing import Optional
from datetime import datetime, timedelta
from curl_cffi.requests import AsyncSession
from ..core.config import config
from ..core.logger import debug_logger


class FileCache:
    """File caching service for videos"""

    def __init__(self, cache_dir: str = "tmp", default_timeout: int = 7200, proxy_manager=None):
        """
        Initialize file cache

        Args:
            cache_dir: Cache directory path
            default_timeout: Default cache timeout in seconds (default: 2 hours)
            proxy_manager: ProxyManager instance for downloading files
        """
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(exist_ok=True)
        self.default_timeout = default_timeout
        self.proxy_manager = proxy_manager
        self._cleanup_task = None

    async def start_cleanup_task(self):
        """Start background cleanup task"""
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def stop_cleanup_task(self):
        """Stop background cleanup task"""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None

    async def _cleanup_loop(self):
        """Background task to clean up expired files"""
        while True:
            try:
                await asyncio.sleep(300)  # Check every 5 minutes
                await self._cleanup_expired_files()
            except asyncio.CancelledError:
                break
            except Exception as e:
                debug_logger.log_error(
                    error_message=f"Cleanup task error: {str(e)}",
                    status_code=0,
                    response_text=""
                )

    async def _cleanup_expired_files(self):
        """Remove expired cache files"""
        try:
            current_time = time.time()
            removed_count = 0

            for file_path in self.cache_dir.iterdir():
                if file_path.is_file():
                    # Check file age
                    file_age = current_time - file_path.stat().st_mtime
                    if file_age > self.default_timeout:
                        try:
                            file_path.unlink()
                            removed_count += 1
                        except Exception:
                            pass

            if removed_count > 0:
                debug_logger.log_info(f"Cleanup: removed {removed_count} expired cache files")

        except Exception as e:
            debug_logger.log_error(
                error_message=f"Failed to cleanup expired files: {str(e)}",
                status_code=0,
                response_text=""
            )

    def _generate_cache_filename(self, url: str, media_type: str) -> str:
        """Generate unique filename for cached file"""
        # Use URL hash as filename
        url_hash = hashlib.md5(url.encode()).hexdigest()

        # Determine file extension
        if media_type == "video":
            ext = ".mp4"
        elif media_type == "image":
            ext = ".jpg"
        else:
            ext = ""

        return f"{url_hash}{ext}"

    async def download_and_cache(self, url: str, media_type: str, max_retries: int = 3) -> str:
        """
        Download file from URL and cache it locally (不使用代理)

        Args:
            url: File URL to download
            media_type: 'image' or 'video'
            max_retries: Maximum retry attempts for 403 errors

        Returns:
            Local cache filename
        """
        filename = self._generate_cache_filename(url, media_type)
        file_path = self.cache_dir / filename

        # Check if already cached and not expired
        if file_path.exists():
            file_age = time.time() - file_path.stat().st_mtime
            if file_age < self.default_timeout:
                debug_logger.log_info(f"Cache hit: {filename}")
                return filename
            else:
                # Remove expired file
                try:
                    file_path.unlink()
                except Exception:
                    pass

        # Download file with retry logic (不使用代理)
        debug_logger.log_info(f"Downloading file from: {url}")

        last_error = None
        for retry_attempt in range(max_retries):
            try:
                result = await self._try_download(url, file_path, None, filename)
                if result:
                    return result
            except Exception as e:
                last_error = e
                error_str = str(e)
                debug_logger.log_warning(f"Download attempt {retry_attempt + 1}/{max_retries} failed: {error_str}")
                
                # Check if it's a 403 error that might benefit from retry
                if "403" in error_str and retry_attempt < max_retries - 1:
                    debug_logger.log_info(f"Got 403 error, retrying...")
                    await asyncio.sleep(1)
                    continue
                elif retry_attempt < max_retries - 1:
                    await asyncio.sleep(0.5)
                    continue
                else:
                    break

        raise Exception(f"Failed to cache file after {max_retries} attempts: {last_error}")

    async def _try_download(self, url: str, file_path: Path, proxy_url: str, filename: str) -> Optional[str]:
        """Try to download file using various methods"""

    async def _try_download(self, url: str, file_path: Path, proxy_url: str, filename: str) -> Optional[str]:
        """Try to download file using various methods (不使用代理)"""
        # Try method 1: curl_cffi with browser impersonation
        try:
            async with AsyncSession() as session:
                headers = {
                    "Accept": "*/*",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "Accept-Encoding": "gzip, deflate, br",
                    "Connection": "keep-alive",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "none",
                    "Upgrade-Insecure-Requests": "1"
                }
                response = await session.get(
                    url,
                    timeout=60,
                    headers=headers,
                    impersonate="chrome120",
                    verify=False
                )

                if response.status_code == 200:
                    with open(file_path, 'wb') as f:
                        f.write(response.content)
                    debug_logger.log_info(f"File cached (curl_cffi): {filename} ({len(response.content)} bytes)")
                    return filename
                elif response.status_code == 403:
                    raise Exception(f"HTTP 403 Forbidden")
                else:
                    debug_logger.log_warning(f"curl_cffi failed with HTTP {response.status_code}, trying wget...")

        except Exception as e:
            if "403" in str(e):
                raise  # Re-raise 403 errors for retry logic
            debug_logger.log_warning(f"curl_cffi failed: {str(e)}, trying wget...")

        # Try method 2: wget command
        try:
            import subprocess

            wget_cmd = [
                "wget",
                "-q",  # Quiet mode
                "-O", str(file_path),  # Output file
                "--timeout=60",
                "--tries=3",
                "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "--header=Accept: */*",
                "--header=Accept-Language: zh-CN,zh;q=0.9,en;q=0.8",
                "--header=Connection: keep-alive"
            ]

            # Add URL
            wget_cmd.append(url)

            # Execute wget
            result = subprocess.run(wget_cmd, capture_output=True, timeout=90)

            if result.returncode == 0 and file_path.exists():
                file_size = file_path.stat().st_size
                if file_size > 0:
                    debug_logger.log_info(f"File cached (wget): {filename} ({file_size} bytes)")
                    return filename
                else:
                    raise Exception("Downloaded file is empty")
            else:
                error_msg = result.stderr.decode('utf-8', errors='ignore') if result.stderr else "Unknown error"
                if "403" in error_msg:
                    raise Exception(f"HTTP 403 Forbidden (wget)")
                debug_logger.log_warning(f"wget failed: {error_msg}, trying curl...")

        except FileNotFoundError:
            debug_logger.log_warning("wget not found, trying curl...")
        except Exception as e:
            if "403" in str(e):
                raise  # Re-raise 403 errors for retry logic
            debug_logger.log_warning(f"wget failed: {str(e)}, trying curl...")

        # Try method 3: system curl command
        try:
            import subprocess

            curl_cmd = [
                "curl",
                "-L",  # Follow redirects
                "-s",  # Silent mode
                "-o", str(file_path),  # Output file
                "--max-time", "60",
                "-H", "Accept: */*",
                "-H", "Accept-Language: zh-CN,zh;q=0.9,en;q=0.8",
                "-H", "Connection: keep-alive",
                "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ]

            # Add URL
            curl_cmd.append(url)

            # Execute curl
            result = subprocess.run(curl_cmd, capture_output=True, timeout=90)

            if result.returncode == 0 and file_path.exists():
                file_size = file_path.stat().st_size
                if file_size > 0:
                    debug_logger.log_info(f"File cached (curl): {filename} ({file_size} bytes)")
                    return filename
                else:
                    raise Exception("Downloaded file is empty")
            else:
                error_msg = result.stderr.decode('utf-8', errors='ignore') if result.stderr else "Unknown error"
                if "403" in error_msg:
                    raise Exception(f"HTTP 403 Forbidden (curl)")
                raise Exception(f"curl command failed: {error_msg}")

        except Exception as e:
            debug_logger.log_error(
                error_message=f"Failed to download file: {str(e)}",
                status_code=0,
                response_text=str(e)
            )
            raise Exception(f"Failed to cache file: {str(e)}")

    def get_cache_path(self, filename: str) -> Path:
        """Get full path to cached file"""
        return self.cache_dir / filename

    def set_timeout(self, timeout: int):
        """Set cache timeout in seconds"""
        self.default_timeout = timeout
        debug_logger.log_info(f"Cache timeout updated to {timeout} seconds")

    def get_timeout(self) -> int:
        """Get current cache timeout"""
        return self.default_timeout

    async def clear_all(self):
        """Clear all cached files"""
        try:
            removed_count = 0
            for file_path in self.cache_dir.iterdir():
                if file_path.is_file():
                    try:
                        file_path.unlink()
                        removed_count += 1
                    except Exception:
                        pass

            debug_logger.log_info(f"Cache cleared: removed {removed_count} files")
            return removed_count

        except Exception as e:
            debug_logger.log_error(
                error_message=f"Failed to clear cache: {str(e)}",
                status_code=0,
                response_text=""
            )
            raise
