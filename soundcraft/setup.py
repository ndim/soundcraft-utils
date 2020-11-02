#
# Copyright (c) 2020 Jim Ramsay <i.am@jimramsay.com>
# Copyright (c) 2020 Hans Ulrich Niedermann <hun@n-dimensional.de>
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.


import abc
import argparse
import io
import shutil
import sys
import time
from pathlib import Path
from string import Template


try:
    import gi  # noqa: F401 'gi' imported but unused
except ModuleNotFoundError:
    print(
        """
The PyGI library must be installed from your distribution; usually called
python-gi, python-gobject, python3-gobject, pygobject, or something similar.
"""
    )
    raise

# We only need the whole gobject and GLib thing here to catch specific exceptions
from gi.repository.GLib import Error as GLibError


import pydbus

import soundcraft

import soundcraft.constants as const

from soundcraft.dirs import get_dirs, init_dirs


class ScriptCommand:
    """A single command which may need to be run in a sudo script

    These commands are to be collected into a shell script.  We do
    *not* run these commands directly via `subprocess.*`.

    Therefore, storing the command as a string containing a shell
    command is a good fit.
    """

    def __init__(self, cmd, skip_if=False, comment=None):
        assert type(cmd) == str
        self.cmd = cmd
        self.skip_if = skip_if
        self.comment = comment

    def write(self, file):
        """write this command to the script file"""
        file.write("\n")
        if self.comment:
            file.write(f"# {self.comment}\n")

        if self.skip_if:
            file.write("# [command skipped (not required)]\n")
            for line in self.cmd.splitlines():
                file.write(f"# {line}\n")
        else:
            file.write(self.cmd)
            file.write("\n")

    def __str__(self):
        return f"ScriptCommand({self.cmd!r})"


class SudoScript:
    """Gather shell commands which must be run by root/sudo into a script

    This script can then be presented to the user to run.

    """

    def __init__(self):
        self.sudo_commands = []

    def add_cmd(self, cmd, skip_if=True, comment=None):
        """Add a command to the sudo script"""
        if skip_if:
            c = ScriptCommand(cmd, skip_if=True, comment=comment)
            print(f"    [skip] {c.cmd!r}")
        else:
            c = ScriptCommand(cmd, skip_if=False, comment=comment)
            print(f"    [q'ed] {c.cmd!r}")
        self.sudo_commands.append(c)

    def needs_to_run(self):
        """The sudo script only needs to run if there are commands in the script"""
        for c in self.sudo_commands:
            if c.skip_if:
                continue  # continue looking for a command to execute
            return True
        return False

    def write(self, file):
        """Write the sudo script to the script file"""
        file.write("#!/bin/sh\n")
        file.write(
            f"# This script contains commands which {const.BASE_EXE_SETUP} could not run.\n"
        )
        file.write(
            "# You might have better luck running them manually, probably via sudo.\n"
        )
        if len(self.sudo_commands) > 0:
            for cmd in self.sudo_commands:
                cmd.write(file)
        else:
            file.write("\n# No commands to run.\n")


SUDO_SCRIPT = SudoScript()


def findDataFiles(subdir):
    """Walk through data files in the soundcraft module's ``data`` subdir``"""

    result = {}
    modulepaths = soundcraft.__path__
    for path in modulepaths:
        path = Path(path)
        datapath = path / "data" / subdir
        result[datapath] = []
        for f in datapath.glob("**/*"):
            if f.is_dir():
                continue  # ignore directories
            result[datapath].append(f.relative_to(datapath))
    return result


class AbstractFile(metaclass=abc.ABCMeta):
    """Common behaviour for different types of files defined as Subclasses"""

    def __init__(self, dst):
        super(AbstractFile, self).__init__()
        self.__dst = Path(dst)

    @property
    def dst(self):
        return self.__dst

    @property
    def chroot_dst(self):
        chroot = get_dirs().chroot
        if chroot:
            return Path(f"{chroot}{self.dst}")
        else:
            return self.dst

    @abc.abstractmethod
    def install(self):
        pass  # AbstractFile.install()

    def uninstall(self):
        # Like many other install/uninstall tools, we just remove the
        # file and leave the directory tree around.
        self.uninstall_msg(self.dst)
        try:
            self.chroot_dst.unlink()
        except FileNotFoundError:
            pass  # No file to remove

    def install_msg(self, dst):
        print("  [inst]", dst)

    def uninstall_msg(self, dst):
        print("  [rm]", dst)


class CopyFile(AbstractFile):
    """This file just needs to be copied from a source file to the destination"""

    def __init__(self, dst, src):
        super(CopyFile, self).__init__(dst)
        self.__src = Path(src)

    @property
    def src(self):
        return self.__src

    def install(self):
        self.install_msg(self.dst)
        self.chroot_dst.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        shutil.copy(self.src, self.chroot_dst)
        self.chroot_dst.chmod(mode=0o644)


class StringToFile(AbstractFile):
    """This destination file is written from a string, no source file required"""

    def __init__(self, dst, content):
        super(StringToFile, self).__init__(dst)

        self.content = content

    def install(self):
        self.install_msg(self.dst)
        self.chroot_dst.parent.mkdir(mode=0o0755, parents=True, exist_ok=True)
        self.chroot_dst.write_text(self.content)
        self.chroot_dst.chmod(mode=0o0644)


class TemplateFile(CopyFile):
    """This destination file is a source file after string template processing"""

    def __init__(self, dst, src, template_data=None):
        super(TemplateFile, self).__init__(dst, src)

        if template_data is None:
            self.template_data = {}
        else:
            self.template_data = template_data

    def install(self):
        self.install_msg(self.dst)
        src_template = Template(self.src.read_text())
        self.chroot_dst.parent.mkdir(mode=0o0755, parents=True, exist_ok=True)
        self.chroot_dst.write_text(src_template.substitute(self.template_data))
        self.chroot_dst.chmod(mode=0o0644)


class AbstractSetup(metaclass=abc.ABCMeta):
    """Things common to subsystem setups"""

    @abc.abstractmethod
    def install(self):
        pass  # AbstractSetup.install()

    @abc.abstractmethod
    def uninstall(self):
        pass  # AbstractSetup.uninstall()


class FileSetup(AbstractSetup):
    """A subsystem which needs to install a number of files"""

    def __init__(self):
        super(FileSetup, self).__init__()
        self.files = []

    def add_file(self, file):
        self.files.append(file)

    def install(self):
        for file in self.files:
            file.install()

    def uninstall(self):
        for file in reversed(self.files):
            file.uninstall()


class DataFileSetup(FileSetup):
    """Some setup which iterates through files in data/"""

    def walk_through_data_files(self, subdir):
        sources = findDataFiles(subdir)
        for (srcpath, files) in sources.items():
            for f in sorted(files):
                src = srcpath / f
                ssrc = str(src)
                if ssrc[-1] == "~":
                    continue  # ignore backup files ending with a tilde
                self.add_src(src)

    @abc.abstractmethod
    def add_src(self, src):
        """Examine src file and decide what to do about it

        Examine the given source file ``src`` and then decide whether
        to

          * ``self.add_file()`` an instance of ``AbstractFile``
          * ``raise UnhandledDataFile(src)``
        """
        pass  # DataFileSetup.add_src()


class UnhandledDataFile(Exception):
    """Unhandled data file encountered while walking through data tree"""

    pass  # class UnhandledDataFile


class SetupDBus(DataFileSetup):
    """Subsystem dealing with the D-Bus configuration files"""

    def __init__(self, no_launch):
        super(SetupDBus, self).__init__()
        self.no_launch = no_launch

        self.walk_through_data_files("dbus-1")

    def add_src(self, src):
        if src.suffix == ".service":
            dirs = get_dirs()
            templateData = {
                "dbus_service_bin": str(dirs.serviceExePath),
                "busname": const.BUSNAME,
            }

            service_dir = dirs.datadir / "dbus-1/services"
            service_dst = service_dir / f"{const.BUSNAME}.service"

            service_file = TemplateFile(service_dst, src, template_data=templateData)
            self.add_file(service_file)
        else:
            raise UnhandledDataFile(src)

    def install(self):
        super(SetupDBus, self).install()

        if not self.no_launch:
            print("Starting D-Bus service as a test...")

            bus = pydbus.SessionBus()
            dbus_service = bus.get(".DBus")
            print(f"Installer version: {soundcraft.__version__}")

            # Give the D-Bus a few seconds to notice the new service file
            timeout = 5
            while True:
                try:
                    dbus_service.StartServiceByName(const.BUSNAME, 0)
                    break  # service has been started, no need to try again
                except GLibError:
                    # If the bus has not recognized the service config file
                    # yet, the service is not bus activatable yet and thus the
                    # GLibError will happen.
                    if timeout == 0:
                        raise
                    timeout = timeout - 1

                    time.sleep(1)
                    continue  # starting service has failed, but try again

            our_service = bus.get(const.BUSNAME)
            service_version = our_service.version
            print(f"Service   version: {service_version}")

            print("Shutting down session D-Bus service...")
            # As the service should either be running at this time or
            # at the very least be bus activatable, we do not catch
            # any exceptions while shutting it down because we want to
            # see any exceptions if they happen.
            our_service.Shutdown()
            print("Session D-Bus service has been shut down")

        print("D-Bus installation is complete")
        print(f"Run {const.BASE_EXE_GUI} or {const.BASE_EXE_CLI} as a regular user")

    def uninstall(self):
        if not self.no_launch:
            bus = pydbus.SessionBus()
            dbus_service = bus.get(".DBus")
            if not dbus_service.NameHasOwner(const.BUSNAME):
                print("D-Bus service not running")
            else:
                print(f"Installer version: {soundcraft.__version__}")
                service = bus.get(const.BUSNAME)
                service_version = service.version
                print(f"Shutting down D-Bus service version {service_version}")
                service.Shutdown()
                print("Session D-Bus service stopped")

        super(SetupDBus, self).uninstall()
        print("D-Bus service is unregistered")


class SetupXDGDesktop(DataFileSetup):
    """Subsystem dealing with the XDG desktop and icon files"""

    # FIXME05: Find out whether `xdg-desktop-menu` and `xdg-desktop-icon`
    #          must be run after all. Fedora Packaging docs suggest so.

    def __init__(self):
        super(SetupXDGDesktop, self).__init__()
        self.walk_through_data_files("xdg")

    def add_src(self, src):
        if src.suffix == ".desktop":
            dirs = get_dirs()
            applications_dir = dirs.datadir / "applications"
            dst = applications_dir / f"{const.APPLICATION_ID}.desktop"
            templateData = {
                "gui_bin": dirs.guiExePath,
                "APPLICATION_ID": const.APPLICATION_ID,
            }
            self.add_file(TemplateFile(dst, src, templateData))
        elif src.suffix == ".png":
            size_suffix = src.suffixes[-2]
            assert size_suffix.startswith(".")
            size = int(size_suffix[1:], 10)
            dst = self.icondir(size) / f"{const.APPLICATION_ID}.png"
            self.add_file(CopyFile(dst, src))
        elif src.suffix == ".svg":
            dst = self.icondir() / f"{const.APPLICATION_ID}.svg"
            self.add_file(CopyFile(dst, src))
        else:
            raise UnhandledDataFile(src)

    def install(self):
        super(SetupXDGDesktop, self).install()
        print("Installed all XDG application launcher files")

    def uninstall(self):
        super(SetupXDGDesktop, self).uninstall()
        print("Removed all XDG application launcher files")

    def icondir(self, size=None):
        dirs = get_dirs()
        if size is None:
            return dirs.datadir / "icons/hicolor/scalable/apps"
        else:
            return dirs.datadir / f"icons/hicolor/{size}x{size}/apps"


class SetupEverything(AbstractSetup):
    """Groups all subsystem setup tasks"""

    def __init__(self):
        super(SetupEverything, self).__init__()
        self.everything = []

    def add(self, thing):
        self.everything.append(thing)

    def install(self):
        for thing in self.everything:
            thing.install()

    def uninstall(self):
        for thing in reversed(self.everything):
            thing.uninstall()


def main():
    parser = argparse.ArgumentParser(
        description=f"Set up/clean up {const.PACKAGE} (install/uninstall)."
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s ({const.PACKAGE}) {soundcraft.__version__}",
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--install",
        help=f"Install and set up {const.PACKAGE} and exit",
        action="store_true",
    )
    group.add_argument(
        "--uninstall",
        help="Undo any installation and setup performed by --install and exit",
        action="store_true",
    )

    parser.add_argument(
        "--no-launch",
        help="when installing, do not test launching the service",
        action="store_true",
    )

    parser.add_argument(
        "--chroot",
        metavar="CHROOT",
        help="chroot dir to (un)install from/into (implies --no-launch)",
        default=None,
    )

    parser.add_argument(
        "--sudo-script",
        metavar="FILENAME",
        help="write the script of sudo commands to the given FILENAME",
        default=None,
    )

    args = parser.parse_args()
    if args.chroot:
        # If chroot is given, the service file is installed inside the chroot
        # and starting/stopping the service does not make sense.
        args.no_launch = True

    if args.chroot and args.sudo_script:
        print("Error: argument --chroot and --sudo-script are mutually exclusive.")
        sys.exit(2)

    if args.chroot:
        print("Using chroot", args.chroot)

    # Initialize the dirs object with the chroot given so that later
    # calls to get_dirs() will yield an object which uses the same
    # chroot value.
    dirs = init_dirs(chroot=args.chroot)
    print("Using dirs", dirs)

    everything = SetupEverything()
    everything.add(SetupDBus(no_launch=args.no_launch))
    everything.add(SetupXDGDesktop())

    if args.install:
        everything.install()
    elif args.uninstall:
        everything.uninstall()

    if args.chroot:
        return

    if (args.sudo_script is None) or (args.sudo_script in ["", "-"]):
        sudo_script_file = io.StringIO()
    else:
        sudo_script_file = open(args.sudo_script, "w")
        p = Path(args.sudo_script)
        p.chmod(0o0755)

    if not SUDO_SCRIPT.needs_to_run():
        print("No commands left over to run with sudo. Good.")
        SUDO_SCRIPT.write(sudo_script_file)
        sys.exit(0)

    SUDO_SCRIPT.write(sudo_script_file)

    if isinstance(sudo_script_file, io.StringIO):
        print("You should probably run the following commands with sudo:")
        print()
        sys.stdout.write(sudo_script_file.getvalue())
        print()
    else:
        sudo_script_file.close()
        print("You should probably run this script with sudo (example command below):")
        print()
        sys.stdout.write(p.read_text())
        print()
        print("Suggested command:", "sudo", p.absolute())
