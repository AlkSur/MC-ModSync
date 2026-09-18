"""SSH/SFTP transport for script A (paramiko).

Spec section: 7.2 ssh.py
- unknown host -> reject by default; explicit accept_new_host allows once
- run() returns (rc, stdout, stderr)
- SFTP helpers: put / stat / mkdirs / remove / rename
"""
from __future__ import annotations

import errno
import os
import posixpath
from typing import Optional, Tuple


class SSHError(Exception):
    exit_code = 5


class HostKeyRejected(SSHError):
    exit_code = 5


class SSHClient:
    def __init__(self) -> None:
        self._client = None
        self._sftp = None

    def connect(self, host: str, port: int, user: str, key_file: str = "",
                password: str = "", known_hosts: str = "",
                accept_new_host: bool = False, timeout: int = 20) -> None:
        import paramiko

        self._client = paramiko.SSHClient()
        policy = paramiko.RejectPolicy()
        missing = paramiko.RejectPolicy()
        if accept_new_host:
            missing = paramiko.AutoAddPolicy()
        self._client.set_missing_host_key_policy(missing)
        kh_path = os.path.expanduser(known_hosts) if known_hosts else None
        try:
            self._client.load_system_host_keys(kh_path)
        except FileNotFoundError:
            pass
        kwargs = dict(
            hostname=host, port=port, username=user,
            timeout=timeout, allow_agent=True, look_for_keys=True,
        )
        if key_file:
            kwargs["key_filename"] = os.path.expanduser(key_file)
        if password:
            kwargs["password"] = password
        try:
            self._client.connect(**kwargs)
        except Exception as e:
            # 覆盖 paramiko.SSHException（含主机密钥被拒）与 socket 层连接错误
            # （NoValidConnectionsError / timeout 等）及认证失败。
            raise SSHError("SSH 连接失败: %s" % e)
        # if not accepting new hosts and server was unknown, connect() would have
        # raised; with AutoAdd the key is now saved in-memory only for this run.
        self._policy_accepted_new = accept_new_host
        _ = policy

    def run(self, cmd: str, timeout: int = 600) -> Tuple[int, str, str]:
        assert self._client is not None, "connect() first"
        try:
            _stdin, stdout, stderr = self._client.exec_command(cmd, timeout=timeout)
            rc = stdout.channel.recv_exit_status()
            out = stdout.read().decode("utf-8", "replace")
            err = stderr.read().decode("utf-8", "replace")
            return rc, out, err
        except Exception as e:  # transport errors
            raise SSHError("SSH 命令执行失败: %s" % e)

    # ---------- SFTP ----------

    def _ensure_sftp(self):
        assert self._client is not None, "connect() first"
        if self._sftp is None:
            self._sftp = self._client.open_sftp()
        return self._sftp

    def stat(self, remote: str) -> Optional[object]:
        try:
            return self._ensure_sftp().stat(remote)
        except IOError as e:
            if e.errno == errno.ENOENT:
                return None
            raise SSHError("SFTP stat 失败: %s" % e)

    def put(self, local: str, remote: str) -> None:
        d = posixpath.dirname(remote)
        if d:
            self.mkdirs(d)
        try:
            self._ensure_sftp().put(local, remote)
        except Exception as e:
            raise SSHError("SFTP 上传失败 %s -> %s: %s" % (local, remote, e))

    def get(self, remote: str, local: str) -> None:
        """SFTP 下载远端文件到本地（自动创建本地父目录）。"""
        d = os.path.dirname(os.path.abspath(local))
        if d:
            os.makedirs(d, exist_ok=True)
        try:
            self._ensure_sftp().get(remote, local)
        except Exception as e:
            raise SSHError("SFTP 下载失败 %s -> %s: %s" % (remote, local, e))

    def remove(self, remote: str) -> None:
        try:
            self._ensure_sftp().remove(remote)
        except IOError as e:
            if e.errno != errno.ENOENT:
                raise SSHError("SFTP 删除失败 %s: %s" % (remote, e))

    def rename(self, old: str, new: str) -> None:
        try:
            self._ensure_sftp().rename(old, new)
        except Exception as e:
            raise SSHError("SFTP 重命名失败 %s -> %s: %s" % (old, new, e))

    def mkdirs(self, remote_dir: str) -> None:
        sftp = self._ensure_sftp()
        parts = remote_dir.strip("/").split("/")
        cur = "" if remote_dir.startswith("/") else "."
        for part in parts:
            cur = (cur + "/" + part) if remote_dir.startswith("/") else posixpath.join(cur, part)
            if not remote_dir.startswith("/") and cur == ".":
                continue
            try:
                sftp.stat(cur)
            except IOError:
                try:
                    sftp.mkdir(cur)
                except IOError as e:
                    raise SSHError("SFTP 建目录失败 %s: %s" % (cur, e))

    def close(self) -> None:
        if self._sftp is not None:
            try:
                self._sftp.close()
            except Exception:
                pass
            self._sftp = None
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
