"""S3-compatible object storage backend (A side; requires boto3).

Spec section: 6, 7.2 storage/s3.py
"""
from __future__ import annotations

from typing import Optional


class S3Error(Exception):
    pass


class S3Store:
    def __init__(self, endpoint_url: str, region: str, bucket: str, prefix: str,
                 access_key: str, secret_key: str, path_style: bool = True) -> None:
        import boto3
        from botocore.config import Config

        self.bucket = bucket
        # prefix stored WITHOUT trailing slash; callers pass keys relative to it
        self.prefix = prefix.strip("/")
        cfg = Config(
            s3={"addressing_style": "path" if path_style else "virtual"},
            retries={"max_attempts": 3, "mode": "standard"},
        )
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region or None,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=cfg,
        )

    def _key(self, key: str) -> str:
        return "%s/%s" % (self.prefix, key.lstrip("/")) if self.prefix else key

    def head(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=self._key(key))
            return True
        except self.client.exceptions.ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                return False
            raise S3Error("HEAD 失败 %s: %s" % (key, e))

    def put_file(self, key: str, path: str, cache_control: str) -> None:
        try:
            self.client.upload_file(
                path, self.bucket, self._key(key),
                ExtraArgs={"CacheControl": cache_control},
            )
        except Exception as e:
            raise S3Error("上传失败 %s: %s" % (key, e))

    def put_bytes(self, key: str, data: bytes, cache_control: str) -> None:
        try:
            self.client.put_object(
                Bucket=self.bucket, Key=self._key(key), Body=data,
                CacheControl=cache_control,
            )
        except Exception as e:
            raise S3Error("上传失败 %s: %s" % (key, e))

    def get_text(self, key: str) -> str:
        try:
            resp = self.client.get_object(Bucket=self.bucket, Key=self._key(key))
            return resp["Body"].read().decode("utf-8")
        except self.client.exceptions.NoSuchKey:
            raise S3Error("对象不存在: %s" % key)
        except Exception as e:
            raise S3Error("读取失败 %s: %s" % (key, e))

    def get_object(self, key: str) -> bytes:
        try:
            resp = self.client.get_object(Bucket=self.bucket, Key=self._key(key))
            return resp["Body"].read()
        except self.client.exceptions.NoSuchKey:
            raise S3Error("对象不存在: %s" % key)
        except Exception as e:
            raise S3Error("读取失败 %s: %s" % (key, e))

    def delete(self, key: str) -> None:
        try:
            self.client.delete_object(Bucket=self.bucket, Key=self._key(key))
        except Exception as e:
            raise S3Error("删除失败 %s: %s" % (key, e))

    def list_objects(self, sub_prefix: str = "") -> "list":
        """列出 prefix/sub_prefix 下的对象（自动翻页）。

        返回 [{"key": <相对 prefix 的键>, "size": int, "last_modified": datetime}]。
        相对键可直接回传给 delete()/get_text()，无需再拼 prefix。
        """
        base = (self.prefix + "/") if self.prefix else ""
        full = base + sub_prefix.lstrip("/").rstrip("/") + "/" if sub_prefix else base
        out = []
        token = None
        while True:
            kw = {"Bucket": self.bucket}
            if full:
                kw["Prefix"] = full
            if token:
                kw["ContinuationToken"] = token
            try:
                resp = self.client.list_objects_v2(**kw)
            except Exception as e:
                raise S3Error("列举对象失败 (prefix=%s): %s" % (full, e))
            for o in resp.get("Contents") or []:
                k = o.get("Key") or ""
                rel = k[len(base):] if base and k.startswith(base) else k
                if not rel:
                    continue
                out.append({"key": rel, "size": int(o.get("Size") or 0),
                            "last_modified": o.get("LastModified")})
            if not resp.get("IsTruncated"):
                break
            token = resp.get("NextContinuationToken")
            if not token:
                break
        return out

    def get_cache_control(self, key: str) -> Optional[str]:
        try:
            resp = self.client.head_object(Bucket=self.bucket, Key=self._key(key))
            return resp.get("CacheControl")
        except self.client.exceptions.ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                return None
            raise S3Error("HEAD 失败 %s: %s" % (key, e))
