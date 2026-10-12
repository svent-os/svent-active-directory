from contextlib import contextmanager
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
import uuid


HERE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('armory_manager', HERE / 'files/manager.py')
manager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manager)


class ArmoryTests(unittest.TestCase):
    def setUp(self):
        self.folder = HERE / ('.test-armory-' + uuid.uuid4().hex)
        self.folder.mkdir()
        self.addCleanup(shutil.rmtree, self.folder)
        self.collection = self.folder / 'collection'
        self.collection.mkdir()
        self.root_patch = patch.object(manager, 'ROOT', self.collection)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.state_patch = patch.object(manager, 'STATE', self.folder / 'state/managed.json')
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)

    def entry(self, category, filename, data):
        return {'category': category, 'filename': filename, 'type': 'file',
                'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}

    @contextmanager
    def temporary(self, **kwargs):
        folder = self.folder / ('stage-' + uuid.uuid4().hex)
        folder.mkdir()
        yield str(folder)

    def test_scripts_and_licenses_are_verified_without_execution(self):
        data = b'#!/bin/sh\nprintf test\n'
        license_data = b'Original upstream license\n'
        entries = [self.entry('linpeas', 'linpeas.sh', data), self.entry('linpeas', 'LICENSE', license_data)]
        personal = self.collection / 'personal.sh'
        personal.write_bytes(b'personal file')
        with patch.object(manager.os, 'geteuid', return_value=0, create=True), patch.object(manager.tempfile, 'TemporaryDirectory', self.temporary), patch.object(manager, 'fetch', side_effect=[data, license_data]):
            manager.configure(entries)
        self.assertEqual((self.collection / 'linpeas/linpeas.sh').read_bytes(), data)
        self.assertEqual((self.collection / 'linpeas/LICENSE').read_bytes(), license_data)
        self.assertEqual(personal.read_bytes(), b'personal file')
        manager.check(entries)
        with patch.object(manager.os, 'geteuid', return_value=0, create=True), patch.object(manager.tempfile, 'TemporaryDirectory', self.temporary), patch.object(manager, 'fetch') as fetch:
            manager.configure(entries)
        fetch.assert_not_called()

    def test_failed_download_does_not_replace_existing_files(self):
        previous = self.collection / 'lse/lse.sh'
        previous.parent.mkdir()
        previous.write_bytes(b'previous version')
        entries = [self.entry('lse', 'lse.sh', b'new version'), self.entry('lse', 'LICENSE', b'license')]
        with patch.object(manager.os, 'geteuid', return_value=0, create=True), patch.object(manager.tempfile, 'TemporaryDirectory', self.temporary), patch.object(manager, 'fetch', side_effect=[b'new version', b'corrupt']):
            with self.assertRaisesRegex(RuntimeError, 'checksum mismatch'):
                manager.configure(entries)
        self.assertEqual(previous.read_bytes(), b'previous version')
        self.assertFalse(manager.STATE.exists())

    def test_linux_resources_have_pinned_origins_and_license_files(self):
        manifest = json.loads((HERE / 'armory-manifest.json').read_text())
        for category, name in [('linpeas', 'linpeas.sh'), ('lse', 'lse.sh')]:
            resources = {entry['filename']: entry for entry in manifest['resources'] if entry['category'] == category}
            self.assertEqual(set(resources), {name, 'LICENSE'})
            for entry in resources.values():
                self.assertNotIn('/latest/', entry['url'])
                self.assertNotIn('/master/', entry['url'])
                self.assertEqual(len(entry['sha256']), 64)
                self.assertGreater(entry['size'], 0)
                self.assertTrue(entry['license_url'].startswith('https://'))

    def test_collection_paths_cannot_escape(self):
        for relative in ['../outside', '/outside', 'lse/../../outside', 'lse\\outside']:
            with self.assertRaises(RuntimeError):
                manager.safe_path(relative)


if __name__ == '__main__':
    unittest.main()
