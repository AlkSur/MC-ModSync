import io
import urllib.error

import pytest

from mcmodsync.storage_probe import CACHE_CONTROL, cache_control_matches, check_storage


@pytest.mark.parametrize('value,expected', [
    (CACHE_CONTROL, True),
    ('must-revalidate,no-store,No-Cache', True),
    (' no-store , must-revalidate , no-cache , private', True),
    ('no-cache', False), (None, False), ('public,max-age=3600', False),
])
def test_cache_header_semantics(value, expected):
    assert cache_control_matches(value) is expected


class Store:
    def __init__(self):
        self.data = {}
        self.cache = CACHE_CONTROL
        self.fail_get = False
        self.deleted = []

    def put_bytes(self, key, data, cache):
        assert cache == CACHE_CONTROL
        self.data[key] = data

    def head(self, key):
        return key in self.data

    def get_text(self, key):
        if self.fail_get:
            raise RuntimeError('secret URL must not appear')
        return self.data[key].decode()

    def get_cache_control(self, key):
        return self.cache

    def delete(self, key):
        self.deleted.append(key)
        self.data.pop(key)


ST = dict(endpointUrl='https://origin.invalid', bucket='b', prefix='/packs/test/',
          accessKey='FAKE', secretKey='FAKE', publicBaseUrl='https://cdn.invalid')


def run(store, opener=None, st=ST):
    reports = []
    if opener is None:
        def opener(request, timeout):
            key = request.full_url.split('/packs/test/', 1)[1]
            return io.BytesIO(store.data[key])
    check_storage(st, lambda *row: reports.append(row),
                  store_factory=lambda *args: store, open_url=opener,
                  sleep=lambda _: None)
    return reports


def test_uses_unique_public_key_inside_pack_and_cleans_up():
    store = Store()
    for _ in range(2):
        assert all(row[1] is True for row in run(store))
    assert not store.data
    assert len(set(store.deleted)) == 2
    assert all(key.startswith('_doctor/') for key in store.deleted)


def test_failure_diagnostics_and_cleanup():
    store = Store()
    store.fail_get = True
    store.cache = None
    reports = run(store)
    assert not store.data
    assert any(row[:2] == ('对象存储内容往返', False) for row in reports)
    assert any(row[:2] == ('对象存储缓存头', False) and 'None' in row[2] for row in reports)
    assert 'secret URL' not in repr(reports)


def test_transient_public_404_retries():
    store = Store()
    attempts = []

    def opener(request, timeout):
        attempts.append(request.full_url)
        if len(attempts) == 1:
            raise urllib.error.HTTPError(request.full_url, 404, 'Not found', {}, None)
        return io.BytesIO(next(iter(store.data.values())))

    reports = run(store, opener)
    assert len(attempts) == 2
    assert any(row[:2] == ('对象存储匿名读', True) for row in reports)


def test_cached_old_body_never_passes():
    store = Store()
    reports = run(store, lambda *args, **kwargs: io.BytesIO(b'doctor'))
    assert any(row[:2] == ('对象存储匿名读', False) for row in reports)
    assert not store.data


def test_public_forbidden_is_reported_and_object_removed():
    store = Store()

    def opener(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 403, 'Forbidden', {}, None)

    reports = run(store, opener)
    assert any(row[:2] == ('对象存储匿名读', False) and '403' in row[2] for row in reports)
    assert not store.data


def test_upload_failure_does_not_claim_delete_success():
    store = Store()

    def fail(*args):
        raise RuntimeError('private credential')

    store.put_bytes = fail
    reports = run(store)
    assert reports == [('对象存储上传', False, 'RuntimeError')]
    assert not store.deleted
