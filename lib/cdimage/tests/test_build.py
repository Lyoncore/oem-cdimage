#! /usr/bin/python

# Copyright (C) 2013 Canonical Ltd.
# Author: Colin Watson <cjwatson@ubuntu.com>

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; version 3 of the License.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""Unit tests for cdimage.build."""

from __future__ import print_function

__metaclass__ = type

import errno
from functools import partial
import gzip
import os
import stat
import subprocess
import sys
from textwrap import dedent
import time
import traceback

try:
    from unittest import mock
except ImportError:
    import mock

from cdimage import osextras
from cdimage.build import (
    _debootstrap_script,
    build_britney,
    build_image_set,
    build_image_set_locked,
    configure_for_project,
    configure_splash,
    extract_debootstrap,
    fix_permissions,
    lock_build_image_set,
    log_marker,
    notify_failure,
    open_log,
    run_debian_cd,
    update_local_indices,
    sync_local_mirror,
)
from cdimage.config import Config
from cdimage.log import logger
from cdimage.mail import text_file_type
from cdimage.tests.helpers import TestCase, touch


class TestUpdateLocalIndices(TestCase):
    def setUp(self):
        super(TestUpdateLocalIndices, self).setUp()
        self.config = Config(read=False)
        self.config.root = self.use_temp_dir()
        self.config["DIST"] = "raring"
        self.config["CPUARCHES"] = "i386"
        self.packages = os.path.join(self.temp_dir, "local", "packages")
        self.database = os.path.join(self.temp_dir, "local", "database")
        self.dists = os.path.join(self.database, "dists")
        self.indices = os.path.join(self.database, "indices")
        self.pool = os.path.join(self.packages, "pool", "local")

    @mock.patch("subprocess.call")
    def test_no_local_packages(self, mock_call):
        self.assertFalse(os.path.exists(self.packages))
        mock_call.side_effect = Exception(
            "subprocess.call called when it should not have been")
        update_local_indices(self.config)

    def test_lists_and_overrides(self):
        fake_dir = os.path.join(self.pool, "f", "fake")
        os.makedirs(fake_dir)
        self.make_deb(
            os.path.join(fake_dir, "fake_1_i386.deb"), "misc", "optional")
        self.make_deb(
            os.path.join(fake_dir, "fake_1_unknown.deb"), "misc", "optional")
        self.make_deb(
            os.path.join(fake_dir, "fake-nf_1_all.deb"),
            "non-free/admin", "extra")
        self.make_deb(
            os.path.join(fake_dir, "fake-udeb_1_i386.udeb"),
            "debian-installer", "optional")
        self.make_deb(
            os.path.join(fake_dir, "fake-udeb_1_unknown.udeb"),
            "debian-installer", "optional")
        self.make_deb(
            os.path.join(fake_dir, "fake-udeb-indep_1_all.udeb"),
            "debian-installer", "extra")
        touch(os.path.join(fake_dir, "random-file"))

        with mock.patch("subprocess.call", return_value=0) as mock_call:
            update_local_indices(self.config)

            expected_command = [
                "apt-ftparchive", "generate", "apt-ftparchive.conf"]
            mock_call.assert_called_once_with(
                expected_command, cwd=self.packages)

        self.assertCountEqual([
            "raring_local_binary-i386.list",
            "raring_local_debian-installer_binary-i386.list",
        ], os.listdir(self.dists))
        with open(os.path.join(
                self.dists, "raring_local_binary-i386.list")) as f:
            self.assertCountEqual([
                "pool/local/f/fake/fake_1_i386.deb",
                "pool/local/f/fake/fake-nf_1_all.deb",
            ], f.read().splitlines())
        with open(os.path.join(
                self.dists,
                "raring_local_debian-installer_binary-i386.list")) as f:
            self.assertCountEqual([
                "pool/local/f/fake/fake-udeb_1_i386.udeb",
                "pool/local/f/fake/fake-udeb-indep_1_all.udeb",
            ], f.read().splitlines())

        self.assertCountEqual([
            "override.raring.local.i386",
            "override.raring.local.debian-installer.i386",
        ], os.listdir(self.indices))
        with open(os.path.join(
                self.indices, "override.raring.local.i386")) as f:
            self.assertCountEqual([
                "fake\toptional\tlocal/misc",
                "fake-nf\textra\tlocal/admin",
            ], f.read().splitlines())
        with open(os.path.join(
                self.indices,
                "override.raring.local.debian-installer.i386")) as f:
            self.assertCountEqual([
                "fake-udeb\toptional\tlocal/debian-installer",
                "fake-udeb-indep\textra\tlocal/debian-installer",
            ], f.read().splitlines())

        self.assertTrue(os.path.exists(os.path.join(
            self.packages, "dists", "raring", "local", "binary-i386")))
        self.assertTrue(os.path.exists(os.path.join(
            self.packages, "dists", "raring", "local", "debian-installer",
            "binary-i386")))


class TestExtractDebootstrap(TestCase):
    def setUp(self):
        super(TestExtractDebootstrap, self).setUp()
        self.config = Config(read=False)
        self.config.root = self.use_temp_dir()

    def test_debootstrap_script(self):
        for series, script in (
            ("gutsy", "usr/lib/debootstrap/scripts/gutsy"),
            ("hardy", "usr/share/debootstrap/scripts/hardy"),
        ):
            self.config["DIST"] = series
            self.assertEqual(script, _debootstrap_script(self.config))

    def test_extract_debootstrap(self):
        self.config["PROJECT"] = "ubuntu"
        self.config["DIST"] = "raring"
        self.config["IMAGE_TYPE"] = "daily"
        self.config["ARCHES"] = "amd64+mac"
        mirror_dir = os.path.join(self.temp_dir, "ftp")
        packages_path = os.path.join(
            mirror_dir, "dists", "raring", "main", "debian-installer",
            "binary-amd64", "Packages.gz")
        udeb_path = os.path.join(
            mirror_dir, "pool", "main", "d", "debootstrap",
            "debootstrap-udeb_1_all.udeb")
        os.makedirs(os.path.dirname(packages_path))
        os.makedirs(os.path.dirname(udeb_path))
        self.make_deb(
            udeb_path, "debian-installer", "extra",
            files={"/usr/share/debootstrap/scripts/raring": b"sentinel"})
        with gzip.GzipFile(packages_path, "wb") as packages:
            ftparchive = subprocess.Popen(
                ["apt-ftparchive", "packages", "pool"],
                stdout=subprocess.PIPE, cwd=mirror_dir)
            data, _ = ftparchive.communicate()
            packages.write(data)
            self.assertEqual(0, ftparchive.returncode)
        extract_debootstrap(self.config)
        output_path = os.path.join(
            self.temp_dir, "scratch", "ubuntu", "raring", "daily",
            "debootstrap", "raring-amd64+mac")
        self.assertTrue(os.path.exists(output_path))
        with open(output_path, "rb") as output:
            self.assertEqual(b"sentinel", output.read())


class TestBuildImageSet(TestCase):
    def setUp(self):
        super(TestBuildImageSet, self).setUp()
        self.config = Config(read=False)
        self.config.root = self.use_temp_dir()
        self.expected_sync_lock = os.path.join(
            self.temp_dir, "etc", ".lock-archive-sync")
        mock_gmtime = mock.patch("time.gmtime", return_value=time.gmtime(0))
        mock_gmtime.start()
        self.addCleanup(mock_gmtime.stop)
        self.epoch_date = "Thu Jan  1 00:00:00 GMT 1970"

    def wait_for_pid(self, pid, expected_status):
        while True:
            try:
                self.assertEqual((pid, expected_status), os.waitpid(pid, 0))
                break
            except OSError as e:
                if e.errno != errno.EINTR:
                    raise

    @mock.patch("subprocess.check_call")
    @mock.patch("cdimage.osextras.unlink_force")
    def test_lock_build_image_set(self, mock_unlink_force, mock_check_call):
        self.config["PROJECT"] = "ubuntu"
        self.config["DIST"] = "raring"
        self.config["IMAGE_TYPE"] = "daily"
        expected_lock_path = os.path.join(
            self.temp_dir, "etc", ".lock-build-image-set-ubuntu-raring-daily")
        self.assertFalse(os.path.exists(expected_lock_path))
        with lock_build_image_set(self.config):
            mock_check_call.assert_called_once_with([
                "lockfile", "-l", "7200", "-r", "0", expected_lock_path])
            self.assertEqual(0, mock_unlink_force.call_count)
        mock_unlink_force.assert_called_once_with(expected_lock_path)

    def test_configure_onlyfree_unsupported(self):
        for project, series, onlyfree, unsupported in (
            ("ubuntu", "raring", False, False),
            ("gobuntu", "hardy", True, False),
            ("edubuntu", "jaunty", False, False),
            ("edubuntu", "karmic", False, True),
            ("xubuntu", "gutsy", False, False),
            ("xubuntu", "hardy", False, True),
            ("kubuntu", "precise", False, False),
            ("kubuntu", "quantal", False, True),
            ("kubuntu-active", "raring", False, True),
            ("ubuntustudio", "raring", False, True),
            ("mythbuntu", "raring", False, True),
            ("lubuntu", "raring", False, True),
            ("ubuntu-moblin-remix", "raring", False, True),
        ):
            config = Config(read=False)
            config["PROJECT"] = project
            config["DIST"] = series
            configure_for_project(config)
            if onlyfree:
                self.assertEqual("1", config["CDIMAGE_ONLYFREE"])
            else:
                self.assertNotIn("CDIMAGE_ONLYFREE", config)
            if unsupported:
                self.assertEqual("1", config["CDIMAGE_UNSUPPORTED"])
            else:
                self.assertNotIn("CDIMAGE_UNSUPPORTED", config)

    def test_configure_install_base(self):
        config = Config(read=False)
        configure_for_project(config)
        self.assertNotIn("CDIMAGE_INSTALL_BASE", config)

        config = Config(read=False)
        config["CDIMAGE_INSTALL"] = "1"
        configure_for_project(config)
        self.assertEqual("1", config["CDIMAGE_INSTALL_BASE"])

    @mock.patch("os.open")
    def test_open_log_debug(self, mock_open):
        self.config["DEBUG"] = "1"
        self.assertIsNone(open_log(self.config))
        self.assertEqual(0, mock_open.call_count)

    def test_open_log_writes_log(self):
        self.config["PROJECT"] = "ubuntu"
        self.config["DIST"] = "raring"
        self.config["IMAGE_TYPE"] = "daily"
        self.config["CDIMAGE_DATE"] = "20130224"
        pid = os.fork()
        if pid == 0:  # child
            log_path = open_log(self.config)
            print("Log path: %s" % log_path)
            print("VERBOSE: %s" % self.config["VERBOSE"])
            sys.stdout.flush()
            print("Standard error", file=sys.stderr)
            sys.stderr.flush()
            os._exit(0)
        else:  # parent
            self.wait_for_pid(pid, 0)
            expected_log_path = os.path.join(
                self.temp_dir, "log", "ubuntu", "raring", "daily-20130224.log")
            self.assertTrue(os.path.exists(expected_log_path))
            with open(expected_log_path) as log:
                self.assertEqual([
                    "Log path: %s" % expected_log_path,
                    "VERBOSE: 3",
                    "Standard error",
                ], log.read().splitlines())

    def test_log_marker(self):
        self.capture_logging()
        log_marker("Testing")
        self.assertLogEqual(["===== Testing =====", self.epoch_date])

    def check_call_make_sync_lock(self, mock_check_call, *args, **kwargs):
        if mock_check_call.call_count == 1:
            self.assertEqual("lockfile", args[0][0])
            osextras.ensuredir(os.path.dirname(self.expected_sync_lock))
            touch(self.expected_sync_lock)
        elif mock_check_call.call_count == 2:
            self.assertEqual("anonftpsync", args[0][0])
            self.assertTrue(os.path.exists(self.expected_sync_lock))

    @mock.patch("subprocess.check_call")
    def test_config_nosync(self, mock_check_call):
        self.config["CAPPROJECT"] = "Ubuntu"
        self.config["CDIMAGE_NOSYNC"] = "1"
        self.capture_logging()
        sync_local_mirror(self.config, 0)
        self.assertLogEqual([])
        self.assertEqual(0, mock_check_call.call_count)

    @mock.patch("subprocess.check_call")
    def test_sync(self, mock_check_call):
        self.config["CAPPROJECT"] = "Ubuntu"
        mock_check_call.side_effect = partial(
            self.check_call_make_sync_lock, mock_check_call)
        self.capture_logging()
        sync_local_mirror(self.config, 0)
        self.assertLogEqual([
            "===== Syncing Ubuntu mirror =====",
            self.epoch_date,
        ])
        mock_check_call.assert_has_calls([
            mock.call(["lockfile", "-r", "4", self.expected_sync_lock]),
            mock.call(["anonftpsync"]),
        ])
        self.assertFalse(os.path.exists(self.expected_sync_lock))

    @mock.patch("subprocess.check_call")
    def test_sync_lock_failure(self, mock_check_call):
        self.config["CAPPROJECT"] = "Ubuntu"
        mock_check_call.side_effect = subprocess.CalledProcessError(1, "")
        self.capture_logging()
        self.assertRaises(
            subprocess.CalledProcessError, sync_local_mirror, self.config, 0)
        self.assertLogEqual([
            "===== Syncing Ubuntu mirror =====",
            self.epoch_date,
            "Couldn't acquire archive sync lock!"
        ])
        mock_check_call.assert_called_once_with(
            ["lockfile", "-r", "4", self.expected_sync_lock])
        self.assertFalse(os.path.exists(self.expected_sync_lock))

    @mock.patch("subprocess.check_call")
    def test_parallel(self, mock_check_call):
        self.config["CAPPROJECT"] = "Ubuntu"
        mock_check_call.side_effect = partial(
            self.check_call_make_sync_lock, mock_check_call)
        self.capture_logging()
        sync_local_mirror(self.config, 1)
        self.assertLogEqual([
            "===== Parallel build; waiting for Ubuntu mirror to sync =====",
            self.epoch_date,
        ])
        mock_check_call.assert_called_once_with(
            ["lockfile", "-8", "-r", "450", self.expected_sync_lock])
        self.assertFalse(os.path.exists(self.expected_sync_lock))

    @mock.patch("subprocess.check_call")
    def test_parallel_lock_failure(self, mock_check_call):
        self.config["CAPPROJECT"] = "Ubuntu"
        mock_check_call.side_effect = subprocess.CalledProcessError(1, "")
        self.capture_logging()
        self.assertRaises(
            subprocess.CalledProcessError, sync_local_mirror, self.config, 1)
        self.assertLogEqual([
            "===== Parallel build; waiting for Ubuntu mirror to sync =====",
            self.epoch_date,
            "Timed out waiting for archive sync lock!"
        ])
        mock_check_call.assert_called_once_with(
            ["lockfile", "-8", "-r", "450", self.expected_sync_lock])
        self.assertFalse(os.path.exists(self.expected_sync_lock))

    @mock.patch("subprocess.check_call")
    def test_build_britney_no_makefile(self, mock_check_call):
        self.capture_logging()
        build_britney(self.config)
        self.assertLogEqual([])
        self.assertEqual(0, mock_check_call.call_count)

    @mock.patch("subprocess.check_call")
    def test_build_britney_with_makefile(self, mock_check_call):
        path = os.path.join(self.temp_dir, "britney", "update_out", "Makefile")
        osextras.ensuredir(os.path.dirname(path))
        touch(path)
        self.capture_logging()
        build_britney(self.config)
        self.assertLogEqual(["===== Building britney =====", self.epoch_date])
        mock_check_call.assert_called_once_with(
            ["make", "-C", os.path.dirname(path)])

    def test_configure_splash(self):
        data_dir = os.path.join(self.temp_dir, "debian-cd", "data", "raring")
        osextras.ensuredir(data_dir)
        for key, extension in (
            ("SPLASHRLE", "rle"),
            ("GFXSPLASH", "pcx"),
            ("SPLASHPNG", "png"),
        ):
            for project_specific in True, False:
                config = Config(read=False)
                config.root = self.temp_dir
                config["PROJECT"] = "kubuntu"
                config["DIST"] = "raring"
                path = os.path.join(
                    data_dir, "%s.%s" % (
                        "kubuntu" if project_specific else "splash",
                        extension))
                touch(path)
                configure_splash(config)
                self.assertEqual(path, config[key])
                osextras.unlink_force(path)

    @mock.patch("subprocess.call", return_value=0)
    def test_run_debian_cd(self, mock_call):
        self.config["CAPPROJECT"] = "Ubuntu"
        self.capture_logging()
        run_debian_cd(self.config)
        self.assertLogEqual([
            "===== Building Ubuntu daily CDs =====",
            self.epoch_date,
        ])
        expected_cwd = os.path.join(self.temp_dir, "debian-cd")
        mock_call.assert_called_once_with(["./build_all.sh"], cwd=expected_cwd)

    def test_fix_permissions(self):
        self.config["PROJECT"] = "ubuntu"
        self.config["DIST"] = "raring"
        self.config["IMAGE_TYPE"] = "daily"
        scratch_dir = os.path.join(
            self.temp_dir, "scratch", "ubuntu", "raring", "daily")
        subdir = os.path.join(scratch_dir, "x")
        dir_one = os.path.join(subdir, "1")
        file_two = os.path.join(subdir, "2")
        file_three = os.path.join(subdir, "3")
        osextras.ensuredir(dir_one)
        touch(file_two)
        touch(file_three)
        for path, perm in (
            (scratch_dir, 0o755),
            (subdir, 0o2775),
            (dir_one, 0o700),
            (file_two, 0o664),
            (file_three, 0o600),
        ):
            os.chmod(path, perm)
        fix_permissions(self.config)
        for path, perm in (
            (scratch_dir, 0o2775),
            (subdir, 0o2775),
            (dir_one, 0o2770),
            (file_two, 0o664),
            (file_three, 0o660),
        ):
            self.assertEqual(perm, stat.S_IMODE(os.stat(path).st_mode))

    @mock.patch("cdimage.build.get_notify_addresses")
    def test_notify_failure_debug(self, mock_notify_addresses):
        self.config["DEBUG"] = "1"
        notify_failure(self.config, None)
        self.assertEqual(0, mock_notify_addresses.call_count)

    @mock.patch("cdimage.build.send_mail")
    def test_notify_failure_no_recipients(self, mock_send_mail):
        notify_failure(self.config, None)
        self.assertEqual(0, mock_send_mail.call_count)

    @mock.patch("cdimage.build.send_mail")
    def test_notify_failure_no_log(self, mock_send_mail):
        self.config["PROJECT"] = "ubuntu"
        self.config["DIST"] = "raring"
        self.config["IMAGE_TYPE"] = "daily"
        self.config["CDIMAGE_DATE"] = "20130225"
        path = os.path.join(self.temp_dir, "production", "notify-addresses")
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as notify_addresses:
            print("ALL\tfoo@example.org", file=notify_addresses)
        notify_failure(self.config, None)
        mock_send_mail.assert_called_once_with(
            "CD image ubuntu/raring/daily failed to build on 20130225",
            "build-image-set", ["foo@example.org"], "")

    @mock.patch("cdimage.build.send_mail")
    def test_notify_failure_log(self, mock_send_mail):
        self.config["PROJECT"] = "ubuntu"
        self.config["DIST"] = "raring"
        self.config["IMAGE_TYPE"] = "daily"
        self.config["CDIMAGE_DATE"] = "20130225"
        path = os.path.join(self.temp_dir, "production", "notify-addresses")
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as notify_addresses:
            print("ALL\tfoo@example.org", file=notify_addresses)
        log_path = os.path.join(self.temp_dir, "log")
        with open(log_path, "w") as log:
            print("Log", file=log)
        notify_failure(self.config, log_path)
        mock_send_mail.assert_called_once_with(
            "CD image ubuntu/raring/daily failed to build on 20130225",
            "build-image-set", ["foo@example.org"], mock.ANY)
        self.assertEqual(log_path, mock_send_mail.call_args[0][3].name)

    def send_mail_to_file(self, path, subject, generator, recipients, body,
                          dry_run=False):
        with open(path, "w") as f:
            print("To: %s" % ", ".join(recipients), file=f)
            print("Subject: %s" % subject, file=f)
            print("X-Generated-By: %s" % generator, file=f)
            print("", file=f)
            if isinstance(body, text_file_type):
                for line in body:
                    print(line.rstrip("\n"), file=f)
            else:
                for line in body.splitlines():
                    print(line, file=f)

    @mock.patch("cdimage.build.sync_local_mirror")
    @mock.patch("cdimage.build.send_mail")
    def test_build_image_set_locked_notifies_on_failure(
            self, mock_send_mail, mock_sync_local_mirror):
        self.config["PROJECT"] = "ubuntu"
        self.config["DIST"] = "raring"
        self.config["IMAGE_TYPE"] = "daily"
        self.config["CDIMAGE_DATE"] = "20130225"
        path = os.path.join(self.temp_dir, "production", "notify-addresses")
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as notify_addresses:
            print("ALL\tfoo@example.org", file=notify_addresses)
        log_path = os.path.join(
            self.temp_dir, "log", "ubuntu", "raring", "daily-20130225.log")
        os.makedirs(os.path.join(self.temp_dir, "etc"))

        def force_failure(*args):
            logger.error("Forced image build failure")
            raise Exception("Artificial exception")

        mock_sync_local_mirror.side_effect = force_failure
        mock_send_mail.side_effect = partial(
            self.send_mail_to_file, os.path.join(self.temp_dir, "mail"))
        pid = os.fork()
        if pid == 0:  # child
            original_stderr = os.dup(sys.stderr.fileno())
            try:
                self.assertFalse(build_image_set_locked(self.config, 0))
            except AssertionError:
                stderr = os.fdopen(original_stderr, "w", 1)
                traceback.print_exc(file=stderr)
                stderr.flush()
                os._exit(1)
            os._exit(0)
        else:  # parent
            self.wait_for_pid(pid, 0)
            with open(log_path) as log:
                self.assertEqual(
                    "Forced image build failure\n", log.readline())
                self.assertEqual(
                    "Traceback (most recent call last):\n", log.readline())
                self.assertIn("Exception: Artificial exception", log.read())

    @mock.patch("subprocess.call", return_value=0)
    @mock.patch("cdimage.build.extract_debootstrap")
    @mock.patch("cdimage.build.Germination.run")
    @mock.patch("cdimage.build.check_installable")
    @mock.patch("cdimage.build.DailyTreePublisher.publish")
    def test_build_image_set_locked(
            self, mock_publish, mock_check_installable, mock_germinate_run,
            mock_extract_debootstrap, mock_call):
        self.config["PROJECT"] = "ubuntu"
        self.config["CAPPROJECT"] = "Ubuntu"
        self.config["DIST"] = "raring"
        self.config["IMAGE_TYPE"] = "daily"
        britney_makefile = os.path.join(
            self.temp_dir, "britney", "update_out", "Makefile")
        os.makedirs(os.path.dirname(britney_makefile))
        touch(britney_makefile)
        os.makedirs(os.path.join(self.temp_dir, "etc"))
        pid = os.fork()
        if pid == 0:  # child
            original_stderr = os.dup(sys.stderr.fileno())
            try:
                self.assertTrue(build_image_set_locked(self.config, 0))
                date = self.config["CDIMAGE_DATE"]
                debian_cd_dir = os.path.join(self.temp_dir, "debian-cd")
                mock_call.assert_has_calls([
                    mock.call(["anonftpsync"]),
                    mock.call([
                        "make", "-C", os.path.dirname(britney_makefile)]),
                    mock.call(["germinate-to-tasks", "daily"]),
                    mock.call(["update-tasks", date, "daily"]),
                    mock.call(["./build_all.sh"], cwd=debian_cd_dir),
                    mock.call(["purge-old-images", "daily"])
                ])
                mock_extract_debootstrap.assert_called_once_with(self.config)
                mock_germinate_run.assert_called_once_with()
                mock_check_installable.assert_called_once_with(self.config)
                mock_publish.assert_called_once_with(date)
            except AssertionError:
                stderr = os.fdopen(original_stderr, "w", 1)
                traceback.print_exc(file=stderr)
                stderr.flush()
                os._exit(1)
            os._exit(0)
        else:  # parent
            self.wait_for_pid(pid, 0)
            log_dir = os.path.join(self.temp_dir, "log", "ubuntu", "raring")
            self.assertTrue(os.path.isdir(log_dir))
            log_entries = os.listdir(log_dir)
            self.assertEqual(1, len(log_entries))
            log_path = os.path.join(log_dir, log_entries[0])
            with open(log_path) as log:
                self.assertEqual(dedent("""\
                    ===== Syncing Ubuntu mirror =====
                    DATE
                    ===== Building britney =====
                    DATE
                    ===== Extracting debootstrap scripts =====
                    DATE
                    ===== Germinating =====
                    DATE
                    ===== Generating new task lists =====
                    DATE
                    ===== Checking for other task changes =====
                    DATE
                    ===== Building Ubuntu daily CDs =====
                    DATE
                    ===== Producing installability report =====
                    DATE
                    ===== Publishing =====
                    DATE
                    ===== Purging old images =====
                    DATE
                    ===== Triggering mirrors =====
                    DATE
                    ===== Finished =====
                    DATE
                    """.replace("DATE", self.epoch_date)), log.read())

    @mock.patch("cdimage.build.build_image_set_locked")
    def test_build_image_set(self, mock_build_image_set_locked):
        self.config["PROJECT"] = "ubuntu"
        self.config["DIST"] = "raring"
        self.config["IMAGE_TYPE"] = "daily"
        lock_path = os.path.join(
            self.temp_dir, "etc", ".lock-build-image-set-ubuntu-raring-daily")
        semaphore_path = os.path.join(
            self.temp_dir, "etc", ".sem-build-image-set")
        os.makedirs(os.path.dirname(lock_path))

        def side_effect(config, semaphore_state):
            self.assertTrue(os.path.exists(lock_path))
            self.assertEqual(0, semaphore_state)
            with open(semaphore_path) as semaphore:
                self.assertEqual("1\n", semaphore.read())

        mock_build_image_set_locked.side_effect = side_effect
        build_image_set(self.config)
