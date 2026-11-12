"""Parse IPTV playlists and keep channels that pass a bounded playback check."""

from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import shutil
import subprocess
from threading import Lock
from urllib.parse import urlsplit
from channel_policy import blocked_channel, blocked_url


@dataclass(frozen=True)
class Channel:
    tags: tuple
    url: str

    @property
    def name(self):
        return self.tags[0].rsplit(",", 1)[-1].strip()

    @property
    def headers(self):
        headers = {}
        for tag in self.tags:
            for option, header in (("http-user-agent", "User-Agent"),
                                   ("http-referrer", "Referer")):
                prefix = f"#EXTVLCOPT:{option}="
                if tag.startswith(prefix):
                    value = tag[len(prefix):]
                    if "\r" not in value and "\n" not in value:
                        headers[header] = value
        return tuple(sorted(headers.items()))


def parse_m3u(content):
    """Keep channel metadata together, including directives before the URL."""
    header = "#EXTM3U"
    channels = []
    tags = None
    for raw in content.lstrip("\ufeff").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTM3U"):
            header = line
        elif line.startswith("#EXTINF:"):
            tags = [line]
        elif tags is not None:
            if line.startswith("#"):
                tags.append(line)
            else:
                channels.append(Channel(tuple(tags), line))
                tags = None
    return header, channels


class StreamValidator:
    def __init__(self, workers=8, timeout=30, sample_seconds=5):
        self.ffmpeg = shutil.which("ffmpeg")
        if not self.ffmpeg:
            raise RuntimeError("需要安装 ffmpeg 后才能验证直播源")
        if workers < 1 or timeout <= 0 or sample_seconds <= 0:
            raise ValueError("workers、timeout、sample_seconds 必须大于 0")
        self.workers = workers
        self.timeout = timeout
        self.sample_seconds = sample_seconds
        # A URL shared by several channels/sources only needs one check per run.
        self.cache = {}
        self._pending = {}
        self._lock = Lock()

    def verify(self, channel):
        """Share one playback check even when repair workers request the same URL."""
        if blocked_channel(channel):
            return False, "频道已屏蔽"
        key = channel.url, channel.headers
        with self._lock:
            if key in self.cache:
                return self.cache[key]
            owner = key not in self._pending
            if owner:
                self._pending[key] = Future()
            future = self._pending[key]
        if owner:
            try:
                result = self.check(*key)
                with self._lock:
                    self.cache[key] = result
                future.set_result(result)
            except Exception as error:
                future.set_exception(error)
        return future.result()

    def check(self, url, headers=()):
        if blocked_url(url):
            return False, "频道已屏蔽"
        try:
            parsed = urlsplit(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                return False, "不支持的播放协议或无效 URL"
        except ValueError:
            return False, "无效 URL"

        command = [
            self.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
            "-xerror", "-rw_timeout", str(int(min(self.timeout, 10) * 1_000_000)),
        ]
        if headers:
            command += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in headers)]
        command += [
            "-i", url, "-map", "0:v:0", "-an", "-sn", "-dn",
            "-t", str(self.sample_seconds), "-progress", "pipe:1",
            "-nostats", "-f", "null", "-",
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True,
                                    errors="replace", timeout=self.timeout)
        except subprocess.TimeoutExpired:
            return False, f"播放验证超时（{self.timeout}s）"
        # Operational errors (e.g. ffmpeg cannot launch) must abort, not empty a list.
        progress = {}
        for line in result.stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                progress[key] = value.strip()
        try:
            frames = int(progress.get("frame", "0"))
            seconds = int(progress.get("out_time_us", "0")) / 1_000_000
        except ValueError:
            frames, seconds = 0, 0
        if result.returncode == 0 and frames > 0 and seconds >= self.sample_seconds - 0.1:
            return True, f"视频解码通过（{seconds:.1f}s）"
        reason = result.stderr.strip().splitlines()
        return False, reason[-1][:250] if reason else "未能解码足够时长的视频"

    def filter_m3u(self, content):
        header, channels = parse_m3u(content)
        pending = {(c.url, c.headers): c for c in channels
                   if not blocked_channel(c) and (c.url, c.headers) not in self.cache}
        print(f"    验证 {len(channels)} 个频道（{len(pending)} 个未检测播放地址）", flush=True)
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {pool.submit(self.verify, channel): key
                       for key, channel in pending.items()}
            for index, future in enumerate(as_completed(futures), 1):
                key = futures[future]
                self.cache[key] = future.result()
                if index % 20 == 0 or index == len(pending):
                    print(f"    已检测 {index}/{len(pending)}", flush=True)
        output = [header]
        kept = 0
        for channel in channels:
            valid, reason = self.verify(channel)
            if valid:
                output.extend(channel.tags)
                output.append(channel.url)
                kept += 1
            else:
                print(f"    丢弃 {channel.tags[0]}: {reason}")
        print(f"    保留频道: {kept}，丢弃频道: {len(channels) - kept}", flush=True)
        return "\n".join(output) + "\n" if kept else None
