#!/usr/bin/env python3
# -*- python -*-
# encoding: utf-8
#
# Adapted munkiimport for Linux
#
# Original Copyright 2010-2025 Greg Neagle.
# Modified for Linux compatibility
#
# Licensed under the Apache License, Version 2.0 (the 'License');
#
# This script assists with importing installer items into a munki repo

import optparse
import os
import subprocess
import sys
import tempfile
import shutil
import datetime
import plistlib
import re
from urllib.parse import unquote
from xml.dom import minidom

# import munkitools
from django.conf import settings
MUNKITOOLS_DIR = settings.MUNKITOOLS_DIR
DEFAULT_CATALOG = settings.DEFAULT_CATALOG

sys.path.append(MUNKITOOLS_DIR)
from munkilib.admin.common import list_items_of_kind
from munkilib.cliutils import pref
from munkilib.wrappers import get_input
from munkilib import munkirepo, munkihash

class RepoCopyError(Exception):
    """Exception raised when copying a file to the repo fails"""
    pass

class PkgInfoGenerationError(Exception):
    """Exception raised when generating pkginfo fails"""
    pass

class CatalogDBException(Exception):
    '''Exception to throw if we can't make a pkginfo DB'''
    #pass

class CatalogReadException(CatalogDBException):
    '''Exception to throw if we can't read the all catalog'''
    #pass


class CatalogDecodeException(CatalogDBException):
    '''Exception to throw if we can't decode the all catalog'''
    #pass

class AttributeDict(dict):
    '''Class that allow us to access foo['bar'] as foo.bar, and return None
    if foo.bar is not defined.'''
    def __getattr__(self, name):
        '''Allow access to dictionary keys as attribute names.'''
        try:
            return super(AttributeDict, self).__getattr__(name)
        except AttributeError:
            try:
                return self[name]
            except KeyError:
                return None

def _cmp(x, y):
    """
    Replacement for built-in function cmp that was removed in Python 3

    Compare the two objects x and y and return an integer according to
    the outcome. The return value is negative if x < y, zero if x == y
    and strictly positive if x > y.
    """
    return (x > y) - (x < y)

class MunkiLooseVersion():
    '''Class based on distutils.version.LooseVersion to compare things like
    "10.6" and "10.6.0" as equal'''

    component_re = re.compile(r'(\d+ | [a-z]+ | \.)', re.VERBOSE)

    def parse(self, vstring):
        """parse function from distutils.version.LooseVersion"""
        # I've given up on thinking I can reconstruct the version string
        # from the parsed tuple -- so I just store the string here for
        # use by __str__
        self.vstring = vstring
        components = [x for x in self.component_re.split(vstring) if x and x != '.']
        for i, obj in enumerate(components):
            try:
                components[i] = int(obj)
            except ValueError:
                pass

        self.version = components

    def __str__(self):
        """__str__ function from distutils.version.LooseVersion"""
        return self.vstring

    def __repr__(self):
        """__repr__ function adapted from distutils.version.LooseVersion"""
        return "MunkiLooseVersion ('%s')" % str(self)

    def __init__(self, vstring=None):
        """init method"""
        if vstring is None:
            # treat None like an empty string
            self.parse('')
        if vstring is not None:
            try:
                try:
                    unicode_type = unicode  # type: ignore[name-defined]
                except NameError:
                    unicode_type = str

                if isinstance(vstring, unicode_type):
                    # unicode string! Why? Oh well...
                    # convert to string so version.LooseVersion doesn't choke
                    vstring = vstring.encode('UTF-8')
            except NameError:
                # python 3
                pass
            self.parse(str(vstring))

    def _pad(self, version_list, max_length):
        """Pad a version list by adding extra 0 components to the end
        if needed"""
        # copy the version_list so we don't modify it
        cmp_list = list(version_list)
        while len(cmp_list) < max_length:
            cmp_list.append(0)
        return cmp_list

    def _compare(self, other):
        """Compare MunkiLooseVersions"""
        if not isinstance(other, MunkiLooseVersion):
            other = MunkiLooseVersion(other)

        max_length = max(len(self.version), len(other.version))
        self_cmp_version = self._pad(self.version, max_length)
        other_cmp_version = self._pad(other.version, max_length)
        cmp_result = 0
        for index, value in enumerate(self_cmp_version):
            try:
                cmp_result = _cmp(value, other_cmp_version[index])
            except TypeError:
                # integer is less than character/string
                if isinstance(value, int):
                    return -1
                return 1
            if cmp_result:
                return cmp_result
        return cmp_result

    def __hash__(self):
        """Hash method"""
        return hash(self.version)

    def __eq__(self, other):
        """Equals comparison"""
        return self._compare(other) == 0

    def __ne__(self, other):
        """Not-equals comparison"""
        return self._compare(other) != 0

    def __lt__(self, other):
        """Less than comparison"""
        return self._compare(other) < 0

    def __le__(self, other):
        """Less than or equals comparison"""
        return self._compare(other) <= 0

    def __gt__(self, other):
        """Greater than comparison"""
        return self._compare(other) > 0

    def __ge__(self, other):
        """Greater than or equals comparison"""
        return self._compare(other) >= 0



def make_pkginfo_metadata():
    '''Records information about the environment in which the pkginfo was
created so we have a bit of an audit trail. Returns a dictionary.'''
    metadata = {}
    metadata['created_by'] = os.getenv("USER", "unknown_user")
    metadata['creation_date'] = datetime.datetime.now().isoformat()
    return metadata


def copy_item_to_repo(repo, itempath, vers, subdirectory=''):
    """Copies an item to the appropriate place in the repo.
    If itempath is a path within the repo/pkgs directory, copies nothing.
    Renames the item if an item already exists with that name.
    Returns the relative path to the item."""

    destination_path = os.path.join('pkgs', subdirectory)
    item_name = os.path.basename(itempath)
    destination_path_name = os.path.join(destination_path, item_name)

    # don't copy if the file is already in the repo
    try:
        if os.path.normpath(repo.local_path(destination_path_name)) == os.path.normpath(itempath):
            # source item is a repo item!
            return destination_path_name
    except AttributeError:
        # no guarantee all repo plugins have the local_path method
        pass

    name, ext = os.path.splitext(item_name)
    if vers:
        if not name.endswith(vers):
            item_name = '%s-%s%s' % (name, vers, ext)
            destination_path_name = os.path.join(destination_path, item_name)

    index = 0
    try:
        pkgs_list = list_items_of_kind(repo, 'pkgs')
    except munkirepo.RepoError as err:
        raise RepoCopyError(u'Unable to get list of current pkgs: %s' % err) from err
    while destination_path_name in pkgs_list:
        index += 1
        item_name = '%s__%s%s' % (name, index, ext)
        destination_path_name = os.path.join(destination_path, item_name)

    try:
        repo.put_from_local_file(destination_path_name, itempath)
    except munkirepo.RepoError as err:
        raise RepoCopyError(u'Unable to copy %s to %s: %s'
                            % (itempath, destination_path_name, err)) from err
    else:
        return destination_path_name


def getiteminfo(itempath):
    """
    Gets info for filesystem items passed to makecatalog item, to be used for
    the "installs" key.
    Determines if the item is an application, bundle, Info.plist, or a file or
    directory and gets additional metadata for later comparison.
    """
    infodict = {}
    if isApplication(itempath):
        infodict['type'] = 'application'
        infodict['path'] = itempath
        plist = getBundleInfo(itempath)
        if plist:
            for key in ['CFBundleName', 'CFBundleIdentifier',
                        'CFBundleShortVersionString', 'CFBundleVersion']:
                if key in plist:
                    infodict[key] = plist[key]
            if 'LSMinimumSystemVersion' in plist:
                infodict['minosversion'] = plist['LSMinimumSystemVersion']
            elif 'LSMinimumSystemVersionByArchitecture' in plist:
                # just grab the highest version if more than one is listed
                versions = [item[1] for item in
                            plist['LSMinimumSystemVersionByArchitecture'].items()]
                highest_version = str(max([MunkiLooseVersion(version)
                                           for version in versions]))
                infodict['minosversion'] = highest_version
            elif 'SystemVersionCheck:MinimumSystemVersion' in plist:
                infodict['minosversion'] = \
                    plist['SystemVersionCheck:MinimumSystemVersion']

    elif (os.path.exists(os.path.join(itempath, 'Contents', 'Info.plist')) or
          os.path.exists(os.path.join(itempath, 'Resources', 'Info.plist'))):
        infodict['type'] = 'bundle'
        infodict['path'] = itempath
        plist = getBundleInfo(itempath)
        for key in ['CFBundleShortVersionString', 'CFBundleVersion']:
            if key in plist:
                infodict[key] = plist[key]

    elif itempath.endswith("Info.plist") or itempath.endswith("version.plist"):
        infodict['type'] = 'plist'
        infodict['path'] = itempath
        try:
            with open(itempath, "rb") as f:
                plist = plistlib.load(f)
            for key in ['CFBundleShortVersionString', 'CFBundleVersion']:
                if key in plist:
                    infodict[key] = plist[key]
        except Exception:
            pass

    # let's help the admin -- if CFBundleShortVersionString is empty
    # or doesn't start with a digit, and CFBundleVersion is there
    # use CFBundleVersion as the version_comparison_key
    if (not infodict.get('CFBundleShortVersionString') or
            infodict['CFBundleShortVersionString'][0]
            not in '0123456789'):
        if infodict.get('CFBundleVersion'):
            infodict['version_comparison_key'] = 'CFBundleVersion'
    elif 'CFBundleShortVersionString' in infodict:
        infodict['version_comparison_key'] = 'CFBundleShortVersionString'

    if ('CFBundleShortVersionString' not in infodict and
            'CFBundleVersion' not in infodict):
        infodict['type'] = 'file'
        infodict['path'] = itempath
        if os.path.isfile(itempath):
            infodict['md5checksum'] = munkihash.getmd5hash(itempath)
    return infodict


def diskImageIsMounted(dmgpath):
    """Check if a DMG file is currently mounted on Linux."""
    with open("/proc/mounts", "r") as mounts:
        for line in mounts:
            if dmgpath in line:
                return True
    return False


def mountdmg(dmgpath, mountpoint=None):
    """Extracts a DMG file on Linux.

    Notes:
    - We don't *mount* disk images here (no root, and often not possible in
      hosted environments). Instead we best-effort extract their contents.
    - Prefer 7-Zip (`7zz`/`7z`) for extraction.
    - If 7-Zip can't read the DMG container, try converting it with `dmg2img`
      (if installed) and extract the resulting image.
    - If extraction returns non-zero but still produced files, treat it as a
      warning (common for DMGs with partial/unsupported sections).
    """

    seven_zip = shutil.which("7zz") or shutil.which("7z")
    dmg2img = shutil.which("dmg2img")
    if not seven_zip and not dmg2img:
        print(
            "Error: no DMG extractor found. Install 7-Zip (`7zz`/`7z`) and/or `dmg2img`.",
            file=sys.stderr,
        )
        return ""

    # Always use a unique extraction directory to avoid collisions across
    # concurrent uploads.
    if not mountpoint:
        base = os.path.splitext(os.path.basename(dmgpath))[0]
        mountpoint = tempfile.mkdtemp(prefix=f"mnt_{base}_")
    else:
        os.makedirs(mountpoint, exist_ok=True)

    def _dir_has_entries(path):
        try:
            return any(True for _ in os.scandir(path))
        except OSError:
            return False

    def _flatten_single_directory(dest):
        try:
            entries = list(os.scandir(dest))
        except OSError:
            return

        if len(entries) != 1:
            return

        only = entries[0]
        if not only.is_dir():
            return

        inner = only.path
        try:
            for child in os.listdir(inner):
                shutil.move(os.path.join(inner, child), os.path.join(dest, child))
            os.rmdir(inner)
        except OSError:
            return

    def _extract_7z(src, dest):
        if not seven_zip:
            return False

        cmd = [seven_zip, "x", src, f"-o{dest}", "-y"]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", errors="replace").strip()
            stdout = proc.stdout.decode("utf-8", errors="replace").strip()
            msg = stderr or stdout
            if msg:
                print(f"Warning extracting {src}: {msg}", file=sys.stderr)

        return _dir_has_entries(dest)

    def _convert_with_dmg2img(src, dest_img):
        if not dmg2img:
            return False
        try:
            cmd = [dmg2img, src, dest_img]
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if proc.returncode != 0:
                stderr = proc.stderr.decode("utf-8", errors="replace").strip()
                stdout = proc.stdout.decode("utf-8", errors="replace").strip()
                msg = stderr or stdout
                if msg:
                    print(f"Warning converting {src} with dmg2img: {msg}", file=sys.stderr)
                return False
            return os.path.exists(dest_img) and os.path.getsize(dest_img) > 0
        except Exception as e:
            print(f"Warning converting {src} with dmg2img: {e}", file=sys.stderr)
            return False

    def _contains_supported_installer(root_dir):
        """Return True if extracted content contains a supported installer item.

        We scan a bit deeper than just the root because many DMGs wrap content
        in a top-level folder.
        """
        try:
            for dirpath, dirnames, filenames in os.walk(root_dir):
                # Limit traversal depth to keep this cheap.
                rel = os.path.relpath(dirpath, root_dir)
                if rel != '.':
                    depth = rel.count(os.sep) + 1
                    if depth > 4:
                        dirnames[:] = []
                        continue

                for name in dirnames:
                    if name.lower().endswith('.app'):
                        return True
                for name in filenames:
                    lower = name.lower()
                    if lower.endswith('.pkg') or lower.endswith('.mpkg'):
                        return True
        except OSError:
            return False
        return False

    # First pass: try extracting the DMG container directly with 7-Zip.
    extracted = _extract_7z(dmgpath, mountpoint)

    # Some DMGs (notably newer Chrome DMGs) will yield only container artifacts
    # (partition map + a .hfs blob) when extracted with older p7zip builds.
    # In that case we still want to try the dmg2img path.
    if extracted and dmg2img and not _contains_supported_installer(mountpoint):
        print(
            f"Info: no .app/.pkg found after 7z extraction of {dmgpath}; trying dmg2img fallback",
            file=sys.stderr,
        )
        extracted = False

    # If 7-Zip couldn't extract anything, try dmg2img conversion (if available).
    if not extracted:
        converted_img = os.path.join(mountpoint, "_converted.img")
        if _convert_with_dmg2img(dmgpath, converted_img):
            inner_dir = os.path.join(mountpoint, "_inner")
            os.makedirs(inner_dir, exist_ok=True)
            extracted = _extract_7z(converted_img, inner_dir)
            if extracted:
                _flatten_single_directory(inner_dir)
                print(f"DMG extracted to {inner_dir}")
                return inner_dir
        else:
            if dmg2img:
                print(
                    f"Warning: dmg2img conversion failed for {dmgpath}",
                    file=sys.stderr,
                )
            else:
                print(
                    f"Warning: dmg2img not installed; cannot fall back for {dmgpath}",
                    file=sys.stderr,
                )

    if not extracted and not _dir_has_entries(mountpoint):
        return ""

    _flatten_single_directory(mountpoint)

    # Optional second pass: if an embedded image file was produced, extract it.
    # Avoid blindly trying to extract arbitrary files (e.g. .DS_Store).
    candidate_exts = {".hfs", ".img", ".iso", ".dmg", ".udif", ".sparseimage", ".apfs"}
    try:
        files = [os.path.join(mountpoint, n) for n in os.listdir(mountpoint)]
        files = [p for p in files if os.path.isfile(p)]
    except OSError:
        files = []

    candidate = None
    for p in files:
        _, ext = os.path.splitext(p)
        if ext.lower() in candidate_exts:
            candidate = p
            break

    if candidate:
        inner_dir = os.path.join(mountpoint, "_inner")
        os.makedirs(inner_dir, exist_ok=True)
        if _extract_7z(candidate, inner_dir):
            _flatten_single_directory(inner_dir)
            print(f"DMG extracted to {inner_dir}")
            return inner_dir

    print(f"DMG extracted to {mountpoint}")
    return mountpoint


def unmountdmg(dmgpath, mountpoint):
    """Cleans up extracted DMG files using 7z."""

    # mountpoint may be returned as a string path; in older call-sites it could
    # also be a list/tuple of mount points.
    if isinstance(mountpoint, (list, tuple)):
        for mp in mountpoint:
            unmountdmg(dmgpath, mp)
        return

    if not os.path.exists(mountpoint):
        print(f"Warning: {mountpoint} does not exist.", file=sys.stderr)
        return

    try:
        shutil.rmtree(mountpoint)
        print(f"{mountpoint} successfully removed.")
    except OSError as e:
        print(f"Warning: Could not remove {mountpoint}: {e}", file=sys.stderr)


def hasValidDiskImageExt(path):
    """Verifies a path ends in '.dmg' or '.iso'"""
    ext = os.path.splitext(path)[1]
    return ext.lower() in ['.dmg', '.iso']


def hasValidPackageExt(path):
    """Verifies a path ends in '.pkg' or '.mpkg'"""
    ext = os.path.splitext(path)[1]
    return ext.lower() in ['.pkg', '.mpkg']


def hasValidInstallerItemExt(path):
    """Verifies we have an installer item"""
    return (hasValidPackageExt(path) or hasValidDiskImageExt(path))


def read_file_or_string(option_value):
    """
    If option_value is a path to a file,
    return contents of file.

    Otherwise, return the string.
    """
    if os.path.exists(os.path.expanduser(option_value)):
        string = readfile(option_value)
    else:
        string = option_value

    return string


def readfile(path):
    '''Reads file at path. Returns a string.'''
    try:
        fileobject = open(os.path.expanduser(path), mode='r', encoding="utf-8")
        data = fileobject.read()
        fileobject.close()
        return data
    except (OSError, IOError):
        print("Couldn't read %s" % path, file=sys.stderr)
        return ""


def makepkginfo(installeritem, options):
    '''Return a pkginfo dictionary for item'''

    if isinstance(options, dict):
        options = AttributeDict(options)

    pkginfo = {}
    installs = []
    if installeritem:
        if not os.path.exists(installeritem):
            raise PkgInfoGenerationError(
                "File %s does not exist" % installeritem)

        # get size of installer item
        itemsize = 0
        itemhash = "N/A"
        if os.path.isfile(installeritem):
            itemsize = int(os.path.getsize(installeritem)/1024)
            try:
                itemhash = munkihash.getsha256hash(installeritem)
            except OSError as err:
                raise PkgInfoGenerationError(err) from err

        if hasValidDiskImageExt(installeritem):
            pkginfo = get_catalog_info_from_dmg(installeritem, options)
            if not pkginfo:
                raise PkgInfoGenerationError(
                    "Could not find a supported installer item in %s!"
                    % installeritem)

        elif hasValidPackageExt(installeritem):
            pkginfo = get_catalog_info_from_path(installeritem, options)
            if not pkginfo:
                raise PkgInfoGenerationError(
                    "%s doesn't appear to be a valid installer item!"
                    % installeritem)
            if os.path.isdir(installeritem) and options.print_warnings:
                print("WARNING: %s is a bundle-style package!\n"
                      "To use it with Munki, you should encapsulate it "
                      "in a disk image.\n" % installeritem, file=sys.stderr)
                # need to walk the dir and add it all up
                for (path, dummy_dirs, files) in os.walk(installeritem):
                    for name in files:
                        filename = os.path.join(path, name)
                        # use os.lstat so we don't follow symlinks
                        itemsize += int(os.lstat(filename).st_size)
                # convert to kbytes
                itemsize = int(itemsize/1024)

        else:
            raise PkgInfoGenerationError(
                "%s is not a valid installer item!" % installeritem)

        pkginfo['installer_item_size'] = itemsize
        if itemhash != "N/A":
            pkginfo['installer_item_hash'] = itemhash

        # try to generate the correct item location
        temppath = installeritem
        location = ""
        while len(temppath) > 4:
            if temppath.endswith('/pkgs'):
                location = installeritem[len(temppath)+1:]
                break
            #else:
            temppath = os.path.dirname(temppath)

        if not location:
            #just the filename
            location = os.path.split(installeritem)[1]
        pkginfo['installer_item_location'] = location

        # No uninstall method yet?
        # if we have receipts, assume we can uninstall using them
        if not pkginfo.get('uninstall_method'):
            if pkginfo.get('receipts'):
                pkginfo['uninstallable'] = True
                pkginfo['uninstall_method'] = "removepackages"
    else:
        if options.nopkg:
            pkginfo['installer_type'] = "nopkg"

    pkginfo['catalogs'] = [DEFAULT_CATALOG]

    default_minosversion = "10.4.0"
    maxfileversion = "0.0.0.0.0"
    if pkginfo:
        pkginfo['autoremove'] = False
        if not 'version' in pkginfo:
            if maxfileversion != "0.0.0.0.0":
                pkginfo['version'] = maxfileversion
            else:
                pkginfo['version'] = "1.0.0.0.0 (Please edit me!)"

    if installs:
        pkginfo['installs'] = installs

    # determine minimum_os_version from identified apps in the installs array
    if pkginfo.get('installer_type') != 'stage_os_installer' and 'installs' in pkginfo:
        # build a list of minosversions using a list comprehension
        item_minosversions = [
            MunkiLooseVersion(item['minosversion'])
            for item in pkginfo['installs']
            if 'minosversion' in item]
        # add the default in case it's an empty list
        item_minosversions.append(
            MunkiLooseVersion(default_minosversion))
        if 'minimum_os_version' in pkginfo:
            # handle case where value may have been set (e.g. flat package)
            item_minosversions.append(MunkiLooseVersion(
                pkginfo['minimum_os_version']))
        # get the maximum from the list and covert back to string
        pkginfo['minimum_os_version'] = str(max(item_minosversions))

    if not 'minimum_os_version' in pkginfo:
        # ensure a minimum_os_version is set unless using --file option only
        pkginfo['minimum_os_version'] = default_minosversion

    # add user/environment metadata
    pkginfo['_metadata'] = make_pkginfo_metadata()

    # return the info
    return pkginfo


def get_catalog_info_from_path(pkgpath, options):
    """Gets package metadata for the package at pathname.
    Returns cataloginfo"""
    cataloginfo = {}
    if os.path.exists(pkgpath):
        cataloginfo = getPackageMetaData(pkgpath)
    return cataloginfo


def get_catalog_info_from_dmg(dmgpath, options):
    """
    * Mounts a disk image if it's not already mounted
    * Gets catalog info for the first installer item found at the root level.
    * Unmounts the disk image if it wasn't already mounted

    To-do: handle multiple installer items on a disk image(?)
    """
    cataloginfo = None
    mountpoint = mountdmg(dmgpath)
    if not mountpoint:
        raise PkgInfoGenerationError("Could not mount %s!" % dmgpath)

    def _walk_limited(root_dir, max_depth=4):
        root_dir = os.path.normpath(root_dir)
        root_depth = root_dir.count(os.sep)
        for dirpath, dirnames, filenames in os.walk(root_dir):
            dirpath = os.path.normpath(dirpath)
            depth = dirpath.count(os.sep) - root_depth
            if depth >= max_depth:
                dirnames[:] = []
            # Avoid hidden/system folders to reduce noise.
            dirnames[:] = [d for d in dirnames if not d.startswith('.')]
            yield dirpath, dirnames, filenames

    def _find_first_package(root_dir):
        candidates = []
        for dirpath, dirnames, filenames in _walk_limited(root_dir, max_depth=4):
            for name in dirnames + filenames:
                lower = name.lower()
                if lower.endswith('.pkg') or lower.endswith('.mpkg'):
                    abspath = os.path.join(dirpath, name)
                    relpath = os.path.relpath(abspath, root_dir)
                    candidates.append((relpath.count(os.sep), abspath, relpath))
        if not candidates:
            return None, None
        _, abspath, relpath = sorted(candidates, key=lambda t: t[0])[0]
        return abspath, relpath

    def _find_first_app(root_dir):
        candidates = []
        for dirpath, dirnames, _filenames in _walk_limited(root_dir, max_depth=4):
            for name in dirnames:
                if name.lower().endswith('.app'):
                    abspath = os.path.join(dirpath, name)
                    relpath = os.path.relpath(abspath, root_dir)
                    candidates.append((relpath.count(os.sep), abspath, relpath))
        if not candidates:
            return None, None
        _, abspath, relpath = sorted(candidates, key=lambda t: t[0])[0]
        return abspath, relpath

    # On Linux, mountdmg() returns a single extraction directory path.
    print("Mounted %s at %s" % (dmgpath, mountpoint))
    if options.pkgname:
        pkgpath = os.path.join(mountpoint, options.pkgname)
        cataloginfo = get_catalog_info_from_path(pkgpath, options)
        if cataloginfo:
            cataloginfo['package_path'] = options.pkgname
    elif not options.item:
        # search for first package at root
        for fsitem in listdir(mountpoint):
            itempath = os.path.join(mountpoint, fsitem)
            if hasValidInstallerItemExt(itempath):
                cataloginfo = get_catalog_info_from_path(itempath, options)
                # get out of fsitem loop
                break

        # Many DMGs wrap the installer in a top-level folder; search deeper.
        if not cataloginfo:
            pkg_abspath, pkg_relpath = _find_first_package(mountpoint)
            if pkg_abspath:
                cataloginfo = get_catalog_info_from_path(pkg_abspath, options)
                if cataloginfo:
                    cataloginfo['package_path'] = pkg_relpath

    if not cataloginfo:
        # maybe this is a drag-n-drop dmg
        # look for given item or an app at the top level of the dmg
        iteminfo = {}
        if options.item:
            item = options.item

            # Create a path by joining the mount point and the provided item
            # path.
            # The os.path.join method will intelligently take care of the
            # following scenarios:
            # ("/mountpoint", "relative/path")  -> "/mountpoint/relative/path"
            # ("/mountpoint", "/absolute/path") -> "/absolute/path"
            itempath = os.path.join(mountpoint, item)

            # Now check that the item actually exists and is located within the
            # mount point
            if os.path.exists(itempath) and os.path.normpath(itempath).startswith(os.path.normpath(mountpoint)):
                iteminfo = getiteminfo(itempath)
            else:
                unmountdmg(dmgpath, mountpoint)
                raise PkgInfoGenerationError(
                    "%s not found on disk image." % item)
        else:
            # no item specified; look for an application at root of
            # mounted dmg
            item = ''
            for itemname in listdir(mountpoint):
                itempath = os.path.join(mountpoint, itemname)
                if isApplication(itempath):
                    item = itemname
                    iteminfo = getiteminfo(itempath)
                    if iteminfo:
                        break

            # Some DMGs place the .app inside a folder; search a bit deeper.
            if not iteminfo:
                app_abspath, app_relpath = _find_first_app(mountpoint)
                if app_abspath and app_relpath:
                    item = app_relpath
                    iteminfo = getiteminfo(app_abspath)

        if iteminfo:
            item_to_copy = {}
            dest_item = None
            if os.path.isabs(item):
                # Absolute path given
                # Remove the mountpoint from item path
                mountpoint_pattern = "^%s/" % re.escape(os.path.normpath(mountpoint))
                rel_item = re.sub(mountpoint_pattern, '', os.path.normpath(item))
                item = rel_item
                dest_item = rel_item
            else:
                dest_item = item

            # Use only the last path component when
            # composing the path key of an installs item
            dest_item_filename = os.path.split(dest_item or item)[1]


            iteminfo['path'] = os.path.join(
                "/Applications", dest_item_filename)
            cataloginfo = {}
            cataloginfo['name'] = iteminfo.get(
                'CFBundleName', os.path.splitext(item)[0])
            version_comparison_key = iteminfo.get(
                'version_comparison_key', "CFBundleShortVersionString")
            cataloginfo['version'] = \
                iteminfo.get(version_comparison_key, "0")
            cataloginfo['installs'] = [iteminfo]
            cataloginfo['installer_type'] = "copy_from_dmg"
            item_to_copy['source_item'] = item
            item_to_copy['destination_path'] = "/Applications"
            cataloginfo['items_to_copy'] = [item_to_copy]
            cataloginfo['uninstallable'] = True
            cataloginfo['uninstall_method'] = "remove_copied_items"

    unmountdmg(dmgpath, mountpoint)
    return cataloginfo


def getPackageMetaData(pkgitem):
    """
    Queries an installer item (.pkg, .mpkg, .dist)
    and gets metadata. There are a lot of valid Apple package formats
    and this function may not deal with them all equally well.
    Standard bundle packages are probably the best understood and documented,
    so this code deals with those pretty well.

    metadata items include:
    installer_item_size:  size of the installer item (.dmg, .pkg, etc)
    installed_size: size of items that will be installed
    RestartAction: will a restart be needed after installation?
    name
    version
    description
    receipts: an array of packageids that may be installed
              (some may not be installed on some machines)
    """

    if not hasValidPackageExt(pkgitem):
        return {}

    # first query /usr/sbin/installer for restartAction
    installerinfo = getPkgRestartInfo(pkgitem)
    # now look for receipt and product version info
    receiptinfo = getReceiptInfo(pkgitem)

    name = os.path.split(pkgitem)[1]
    shortname = os.path.splitext(name)[0]
    metaversion = getBundleVersion(pkgitem)
    if metaversion == '0.0.0.0.0':
        metaversion = nameAndVersion(shortname)[1] or '0.0.0.0.0'

    highestpkgversion = '0.0'
    installedsize = 0
    for infoitem in receiptinfo['receipts']:
        if (MunkiLooseVersion(infoitem['version']) >
                MunkiLooseVersion(highestpkgversion)):
            highestpkgversion = infoitem['version']
        if 'installed_size' in infoitem:
            # note this is in KBytes
            installedsize += infoitem['installed_size']

    if metaversion == '0.0.0.0.0':
        metaversion = highestpkgversion
    elif len(receiptinfo['receipts']) == 1:
        # there is only one package in this item
        metaversion = highestpkgversion
    elif highestpkgversion.startswith(metaversion):
        # for example, highestpkgversion is 2.0.3124.0,
        # version in filename is 2.0
        metaversion = highestpkgversion

    cataloginfo = {}
    cataloginfo['name'] = nameAndVersion(shortname)[0]
    cataloginfo['version'] = receiptinfo.get("product_version") or metaversion
    for key in ('display_name', 'RestartAction', 'description'):
        if key in installerinfo:
            cataloginfo[key] = installerinfo[key]

    if 'installed_size' in installerinfo:
        if installerinfo['installed_size'] > 0:
            cataloginfo['installed_size'] = installerinfo['installed_size']
    elif installedsize:
        cataloginfo['installed_size'] = installedsize

    cataloginfo['receipts'] = receiptinfo['receipts']

    if os.path.isfile(pkgitem) and not pkgitem.endswith('.dist'):
        # flat packages require 10.5.0+
        cataloginfo['minimum_os_version'] = "10.5.0"

    return cataloginfo


def getChoiceChangesXML(pkgitem):
    """Best-effort extraction of choice data from a macOS .pkg under Linux.

    Note: macOS "Distribution" files are generally *not* plists, so attempting
    to parse them with plistlib can fail. This function is currently unused in
    this repo; keep it safe and quiet to avoid noisy logs during uploads.
    """
    choices = []

    # check if `7z` is installed
    if not shutil.which("7z"):
        print("Error: `7z` is not installed. Install it with `apt install p7zip-full`.", file=sys.stderr)
        return choices

    # tmp dir to unzip
    temp_dir = tempfile.mkdtemp()

    try:
        # Unzip .pkg with `7z`
        cmd = ["7z", "x", pkgitem, f"-o{temp_dir}"]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode != 0:
            print(f"Error extracting {pkgitem}: {proc.stderr.decode('utf-8')}", file=sys.stderr)
            return choices

        # The `Distribution` file is typically XML, not a plist.
        distribution_path = os.path.join(temp_dir, "Distribution")
        if os.path.exists(distribution_path):
            try:
                with open(distribution_path, "rb") as f:
                    plist_data = plistlib.load(f)
                if isinstance(plist_data, list):
                    # Only keep dict-like entries; ignore nulls/non-dicts.
                    choices = [
                        item for item in plist_data
                        if isinstance(item, dict) and 'selected' in str(item.get('choiceAttribute', ''))
                    ]
            except Exception:
                # Expected for most packages (Distribution isn't a plist).
                choices = []

    except Exception:
        # Best-effort only; don't spam stderr for metadata we can live without.
        choices = []

    finally:
        # Clean up the temporary directory.
        shutil.rmtree(temp_dir, ignore_errors=True)

    return choices


def isApplication(pathname):
    """Return True if path appears to be a macOS app bundle.

    For Linux DMG extraction we primarily rely on the presence of
    `Contents/Info.plist` (or `Resources/Info.plist`) because some extraction
    tools omit binaries/symlinks and the executable may not exist.
    """
    if not pathname:
        return False
    if os.path.islink(pathname):
        return False
    if not os.path.isdir(pathname):
        return False
    if not pathname.endswith('.app'):
        return False

    plist = getBundleInfo(pathname)
    if not isinstance(plist, dict) or not plist:
        return False

    # If present, validate the package type. If missing, still accept.
    pkg_type = plist.get('CFBundlePackageType')
    if pkg_type and pkg_type != 'APPL':
        return False

    return True


def listdir(path):
    """OS X HFS+ string encoding safe listdir().

    Args:
        path: path to list contents of
    Returns:
        list of contents, items as str or unicode types
    """
    # if os.listdir() is supplied a unicode object for the path,
    # it will return unicode filenames instead of their raw fs-dependent
    # version, which is decomposed utf-8 on OS X.
    #
    # we use this to our advantage here and have Python do the decoding
    # work for us, instead of decoding each item in the output list.
    #
    # references:
    # https://docs.python.org/howto/unicode.html#unicode-filenames
    # https://developer.apple.com/library/mac/#qa/qa2001/qa1235.html
    # http://lists.zerezo.com/git/msg643117.html
    # http://unicode.org/reports/tr15/    section 1.2
    # pylint: disable=unicode-builtin
    # Python 2 compatibility helpers in a Python 3 runtime can raise NameError
    # when referencing `unicode`. Keep this safe and minimal.
    try:
        unicode_type = unicode  # type: ignore[name-defined]
    except NameError:
        unicode_type = str

    if isinstance(path, bytes):
        try:
            path = path.decode('utf-8')
        except Exception:
            path = path.decode(errors='replace')

    if not isinstance(path, (str, unicode_type)):
        path = str(path)

    return os.listdir(path)


def getBundleInfo(path):
    """Returns Info.plist data if available for bundle at path (Linux-compatible)."""
    infopath = os.path.join(path, "Contents", "Info.plist")
    if not os.path.exists(infopath):
        infopath = os.path.join(path, "Resources", "Info.plist")

    if os.path.exists(infopath):
        try:
            with open(infopath, "rb") as f:
                plist = plistlib.load(f)
                return plist
        except Exception as e:
            print(f"Error reading plist file {infopath}: {e}", file=sys.stderr)

    return None


def getVersionString(plist, key=None):
    """Gets a version string from the plist.

    If a key is explicitly specified, the value of that key is returned without
    modification, or an empty string if the key does not exist.

    If key is not specified:
    if there's a valid CFBundleShortVersionString, returns that.
    else if there's a CFBundleVersion, returns that
    else returns an empty string.

    """
    VersionString = ''
    if key:
        # admin has specified a specific key
        # return value verbatim or empty string
        return plist.get(key, '')

    # default to CFBundleShortVersionString plus magic
    # and workarounds and edge case cleanups
    key = 'CFBundleShortVersionString'
    if not 'CFBundleShortVersionString' in plist:
        if 'Bundle versions string, short' in plist:
            # workaround for broken Composer packages
            # where the key is actually named
            # 'Bundle versions string, short' instead of
            # 'CFBundleShortVersionString'
            key = 'Bundle versions string, short'
    if plist.get(key):
        # return key value up to first space
        # lets us use crappy values like '1.0 (100)'
        VersionString = plist[key].split()[0]
    if VersionString:
        # check first character to see if it's a digit
        if VersionString[0] in '0123456789':
            # starts with a number; that's good
            # now for another edge case thanks to Adobe:
            # replace commas with periods
            VersionString = VersionString.replace(',', '.')
            return VersionString
    if plist.get('CFBundleVersion'):
        # no CFBundleShortVersionString, or bad one
        # a future version of the Munki tools may drop this magic
        # and require admins to explicitly choose the CFBundleVersion
        # but for now Munki does some magic
        VersionString = plist['CFBundleVersion'].split()[0]
        # check first character to see if it's a digit
        if VersionString[0] in '0123456789':
            # starts with a number; that's good
            # now for another edge case thanks to Adobe:
            # replace commas with periods
            VersionString = VersionString.replace(',', '.')
            return VersionString
    return ''


def getPkgRestartInfo(filename):
    """Extracts RestartAction info from a macOS .pkg under Linux using 7z."""
    installerinfo = {}

    # Check if `7z` is installed
    if not shutil.which("7z"):
        print("Error: `7z` is not installed. Install it with `apt install p7zip-full`.", file=sys.stderr)
        return installerinfo

    # Temporary directory for extracting the package
    temp_dir = tempfile.mkdtemp()

    try:
        # Extract the .pkg using `7z`
        cmd = ["7z", "x", filename, f"-o{temp_dir}"]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode != 0:
            print(f"Error extracting {filename}: {proc.stderr.decode('utf-8')}", file=sys.stderr)
            return installerinfo

        # Look for `Distribution` or `PackageInfo`
        for possible_file in ["Distribution", "PackageInfo"]:
            file_path = os.path.join(temp_dir, possible_file)
            if os.path.exists(file_path):
                # These files are usually XML (not plists). Try plist first (in
                # case a tool produced a plist), then fall back to XML parsing.
                restart_action = None

                try:
                    with open(file_path, "rb") as f:
                        parsed = plistlib.load(f)
                    if isinstance(parsed, dict):
                        restart_action = parsed.get("RestartAction")
                except Exception:
                    restart_action = None

                if not restart_action:
                    try:
                        dom = minidom.parse(file_path)
                        # Common tag spellings seen in package metadata.
                        for tag in ("restartAction", "restart-action"):
                            nodes = dom.getElementsByTagName(tag)
                            if nodes and nodes[0].firstChild:
                                restart_action = nodes[0].firstChild.wholeText.strip()
                                break
                    except Exception:
                        restart_action = None

                if restart_action and restart_action != "None":
                    installerinfo["RestartAction"] = restart_action
                    break  # Found, stop searching

    except Exception:
        # RestartAction is optional metadata; don't spam stderr.
        installerinfo = {}

    finally:
        # Cleanup temp directory
        shutil.rmtree(temp_dir, ignore_errors=True)

    return installerinfo

def getReceiptInfo(pkgname):
    """Get receipt info (a dict) from a package"""
    # Always return a dict with at least a 'receipts' key so callers can be
    # defensive without needing to special-case failures.
    info = {"receipts": [], "product_version": None}

    if hasValidPackageExt(pkgname):
        if os.path.isfile(pkgname):  # flat package (XAR)
            flat_info = getFlatPackageInfo(pkgname)
            if isinstance(flat_info, dict):
                # Keep only known keys to avoid unexpected shapes.
                info["receipts"] = flat_info.get("receipts") or []
                info["product_version"] = flat_info.get("product_version")
            elif isinstance(flat_info, list):
                info["receipts"] = flat_info

        elif os.path.isdir(pkgname):  # bundle-style package
            bundle_info = getBundlePackageInfo(pkgname)
            if isinstance(bundle_info, dict):
                info["receipts"] = bundle_info.get("receipts") or []
                info["product_version"] = bundle_info.get("product_version")

    elif pkgname.endswith('.dist'):
        info["receipts"] = parsePkgRefs(pkgname)

    if info.get("receipts") is None:
        info["receipts"] = []
    return info


def getBundleVersion(bundlepath, key=None):
    """
    Returns version number from a bundle.
    Some extra code to deal with very old-style bundle packages

    Specify key to use a specific key in the Info.plist for the version string.
    """
    plist = getBundleInfo(bundlepath)
    if plist:
        versionstring = getVersionString(plist, key)
        if versionstring:
            return versionstring

    # no version number in Info.plist. Maybe old-style package?
    infopath = os.path.join(
        bundlepath, 'Contents', 'Resources', 'English.lproj')
    if os.path.exists(infopath):
        for item in listdir(infopath):
            if os.path.join(infopath, item).endswith('.info'):
                infofile = os.path.join(infopath, item)
                infodict = parseInfoFile(infofile)
                return infodict.get("Version", "0.0.0.0.0")

    # didn't find a version number, so return 0...
    return '0.0.0.0.0'


def parseInfoFile(infofile):
    '''Returns a dict of keys and values parsed from an .info file
    At least some of these old files use MacRoman encoding...'''
    infodict = {}
    fileobj = open(infofile, mode='rb')
    info = fileobj.read()
    fileobj.close()
    infolines = info.splitlines()
    for line in infolines:
        try:
            parts = line.split(None, 1)
            if len(parts) == 2:
                try:
                    key = parts[0].decode("mac_roman")
                except (LookupError, UnicodeDecodeError):
                    key = parts[0].decode("UTF-8")
                try:
                    value = parts[1].decode("mac_roman")
                except (LookupError, UnicodeDecodeError):
                    value = parts[1].decode("UTF-8")
                infodict[key] = value
        except UnicodeDecodeError:
            # something we could not handle; just skip it
            pass
    return infodict


def nameAndVersion(aString):
    """
    Splits a string into the name and version numbers:
    'TextWrangler2.3b1' becomes ('TextWrangler', '2.3b1')
    'AdobePhotoshopCS3-11.2.1' becomes ('AdobePhotoshopCS3', '11.2.1')
    'MicrosoftOffice2008v12.2.1' becomes ('MicrosoftOffice2008', '12.2.1')
    """
    # first try regex
    m = re.search(r'[0-9]+(\.[0-9]+)((\.|a|b|d|v)[0-9]+)+', aString)
    if m:
        vers = m.group(0)
        name = aString[0:aString.find(vers)].rstrip(' .-_v')
        return (name, vers)

    # try another way
    index = 0
    for char in aString[::-1]:
        if char in '0123456789._':
            index -= 1
        elif char in 'abdv':
            partialVersion = aString[index:]
            if set(partialVersion).intersection(set('abdv')):
                # only one of 'abdv' allowed in the version
                break
            index -= 1
        else:
            break

    if index < 0:
        possibleVersion = aString[index:]
        # now check from the front of the possible version until we
        # reach a digit (because we might have characters in '._abdv'
        # at the start)
        for char in possibleVersion:
            if not char in '0123456789':
                index += 1
            else:
                break
        vers = aString[index:]
        return (aString[0:index].rstrip(' .-_v'), vers)
    # no version number found,
    # just return original string and empty string
    return (aString, '')


def getFlatPackageInfo(pkgpath):
    """
    Returns an array of dictionaries with info on subpackages
    contained in the flat package using 7z instead of xar.
    """
    receiptarray = []
    abspkgpath = os.path.abspath(pkgpath)
    pkgtmp = tempfile.mkdtemp()
    cwd = os.getcwd()

    # Check for 7z. Some deployments (notably Azure App Service zip deploy)
    # don't provide it and you typically can't apt-get at runtime.
    # Return a minimal shape so callers can proceed.
    if not shutil.which("7z"):
        print(
            "Warning: `7z` is not installed; skipping receipt extraction for flat packages.",
            file=sys.stderr,
        )
        return {"receipts": [], "product_version": None}

    try:
        # Extract the .pkg using `7z`
        cmd_extract = ["7z", "x", abspkgpath, f"-o{pkgtmp}"]
        proc = subprocess.run(cmd_extract, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode != 0:
            print(
                f"Error extracting {pkgpath}: {proc.stderr.decode('utf-8', errors='replace')}",
                file=sys.stderr,
            )
            return {"receipts": [], "product_version": None}

        # Search for `PackageInfo` files
        for root, _, files in os.walk(pkgtmp):
            for file in files:
                if file == "PackageInfo":
                    packageinfoabspath = os.path.abspath(os.path.join(root, file))
                    receiptarray.extend(parsePkgRefs(packageinfoabspath))

        # If no `PackageInfo` files were found, look for `Distribution`
        if not receiptarray:
            for root, _, files in os.walk(pkgtmp):
                for file in files:
                    if file == "Distribution":
                        distributionabspath = os.path.abspath(os.path.join(root, file))
                        receiptarray = parsePkgRefs(distributionabspath, path_to_pkg=pkgpath)
                        break

        if not receiptarray:
            print(
                "Warning: No receipts found in Distribution or PackageInfo files within the package.",
                file=sys.stderr
            )

        # Extract product version
        productversion = None
        for root, _, files in os.walk(pkgtmp):
            for file in files:
                if file == "Distribution":
                    distributionabspath = os.path.abspath(os.path.join(root, file))
                    productversion = getProductVersionFromDist(distributionabspath)

    except Exception as e:
        print(f"Error processing {pkgpath}: {e}", file=sys.stderr)
        return {"receipts": [], "product_version": None}

    finally:
        os.chdir(cwd)  # Switch back to the original working directory
        shutil.rmtree(pkgtmp)  # Remove temporary directory

    info = {
        "receipts": receiptarray,
        "product_version": productversion
    }
    return info


def parsePkgRefs(filename, path_to_pkg=None):
    """Parses a .dist or PackageInfo file looking for pkg-ref or pkg-info tags
    to get info on included sub-packages"""
    info = []
    dom = minidom.parse(filename)
    pkgrefs = dom.getElementsByTagName('pkg-info')
    if pkgrefs:
        # this is a PackageInfo file
        for ref in pkgrefs:
            keys = list(ref.attributes.keys())
            if 'identifier' in keys and 'version' in keys:
                pkginfo = {}
                pkginfo['packageid'] = \
                       ref.attributes['identifier'].value
                pkginfo['version'] = \
                    ref.attributes['version'].value
                payloads = ref.getElementsByTagName('payload')
                if payloads:
                    keys = list(payloads[0].attributes.keys())
                    if 'installKBytes' in keys:
                        pkginfo['installed_size'] = int(float(
                            payloads[0].attributes[
                                'installKBytes'].value))
                    if pkginfo not in info:
                        info.append(pkginfo)
                # if there isn't a payload, no receipt is left by a flat
                # pkg, so don't add this to the info array
    else:
        pkgrefs = dom.getElementsByTagName('pkg-ref')
        if pkgrefs:
            # this is a Distribution or .dist file
            pkgref_dict = {}
            for ref in pkgrefs:
                keys = list(ref.attributes.keys())
                if 'id' in keys:
                    pkgid = ref.attributes['id'].value
                    if not pkgid in pkgref_dict:
                        pkgref_dict[pkgid] = {'packageid': pkgid}
                    if 'version' in keys:
                        pkgref_dict[pkgid]['version'] = \
                            ref.attributes['version'].value
                    if 'installKBytes' in keys:
                        pkgref_dict[pkgid]['installed_size'] = int(float(
                            ref.attributes['installKBytes'].value))
                    if ref.firstChild:
                        text = ref.firstChild.wholeText
                        if text.endswith('.pkg'):
                            if text.startswith('file:'):
                                relativepath = unquote(text[5:])
                                pkgdir = os.path.dirname(
                                    path_to_pkg or filename)
                                pkgref_dict[pkgid]['file'] = os.path.join(
                                    pkgdir, relativepath)
                            else:
                                if text.startswith('#'):
                                    text = text[1:]
                                relativepath = unquote(text)
                                thisdir = os.path.dirname(filename)
                                pkgref_dict[pkgid]['file'] = os.path.join(
                                    thisdir, relativepath)

            for (key, pkgref) in pkgref_dict.items():
                if 'file' in pkgref:
                    if os.path.exists(pkgref['file']):
                        receipts = getReceiptInfo(
                            pkgref['file']).get("receipts", [])
                        info.extend(receipts)
                        continue
                if 'version' in pkgref:
                    if 'file' in pkgref:
                        del pkgref['file']
                    info.append(pkgref_dict[key])

    return info


def getBundlePackageInfo(pkgpath):
    """Get metadata from a bundle-style package"""
    receiptarray = []

    if pkgpath.endswith('.pkg'):
        pkginfo = getOnePackageInfo(pkgpath)
        if pkginfo:
            receiptarray.append(pkginfo)
            return {"receipts": receiptarray}

    bundlecontents = os.path.join(pkgpath, 'Contents')
    if os.path.exists(bundlecontents):
        for item in listdir(bundlecontents):
            if item.endswith('.dist'):
                filename = os.path.join(bundlecontents, item)
                # return info using the distribution file
                receiptarray = parsePkgRefs(filename, path_to_pkg=bundlecontents)
                return {"receipts": receiptarray}

        # no .dist file found, look for packages in subdirs
        dirsToSearch = []
        plist = getBundleInfo(pkgpath)
        if plist:
            if 'IFPkgFlagComponentDirectory' in plist:
                componentdir = plist['IFPkgFlagComponentDirectory']
                dirsToSearch.append(componentdir)

        if not dirsToSearch:
            dirsToSearch = ['', 'Contents', 'Contents/Installers',
                            'Contents/Packages', 'Contents/Resources',
                            'Contents/Resources/Packages']
        for subdir in dirsToSearch:
            searchdir = os.path.join(pkgpath, subdir)
            if os.path.exists(searchdir):
                for item in listdir(searchdir):
                    itempath = os.path.join(searchdir, item)
                    if os.path.isdir(itempath):
                        if itempath.endswith('.pkg'):
                            pkginfo = getOnePackageInfo(itempath)
                            if pkginfo:
                                receiptarray.append(pkginfo)
                        elif itempath.endswith('.mpkg'):
                            pkginfo = getBundlePackageInfo(itempath)
                            if pkginfo:
                                receiptarray.extend(pkginfo.get("receipts"))

    return {"receipts": receiptarray}


def getOnePackageInfo(pkgpath):
    """Gets receipt info for a single bundle-style package"""
    pkginfo = {}
    plist = getBundleInfo(pkgpath)
    if plist:
        pkginfo['filename'] = os.path.basename(pkgpath)
        try:
            if 'CFBundleIdentifier' in plist:
                pkginfo['packageid'] = plist['CFBundleIdentifier']
            elif 'Bundle identifier' in plist:
                # special case for JAMF Composer generated packages.
                pkginfo['packageid'] = plist['Bundle identifier']
            else:
                pkginfo['packageid'] = os.path.basename(pkgpath)

            if 'CFBundleName' in plist:
                pkginfo['name'] = plist['CFBundleName']

            if 'IFPkgFlagInstalledSize' in plist:
                pkginfo['installed_size'] = int(plist['IFPkgFlagInstalledSize'])

            pkginfo['version'] = getBundleVersion(pkgpath)
        except (AttributeError, KeyError):
            pkginfo['packageid'] = 'BAD PLIST in %s' % \
                                    os.path.basename(pkgpath)
            pkginfo['version'] = '0.0'
    else:
        # look for old-style .info files!
        infopath = os.path.join(
            pkgpath, 'Contents', 'Resources', 'English.lproj')
        if os.path.exists(infopath):
            for item in listdir(infopath):
                if os.path.join(infopath, item).endswith('.info'):
                    pkginfo['filename'] = os.path.basename(pkgpath)
                    pkginfo['packageid'] = os.path.basename(pkgpath)
                    infofile = os.path.join(infopath, item)
                    infodict = parseInfoFile(infofile)
                    pkginfo['version'] = infodict.get('Version', '0.0')
                    pkginfo['name'] = infodict.get('Title', 'UNKNOWN')
                    break
    return pkginfo


def getProductVersionFromDist(filename):
    """Extracts product version from a Distribution file"""
    dom = minidom.parse(filename)
    product = dom.getElementsByTagName('product')
    if product:
        keys = list(product[0].attributes.keys())
        if "version" in keys:
            return product[0].attributes["version"].value
    return None


def find_matching_pkginfo(repo, pkginfo):
    """Looks through repo catalogs looking for matching pkginfo
    Returns a pkginfo dictionary, or an empty dict"""

    try:
        catdb = make_catalog_db(repo)
    except CatalogReadException as err:
        # could not retrieve catalogs/all
        # do we have any existing pkgsinfo items?
        pkgsinfo_items = repo.itemlist('pkgsinfo')
        if pkgsinfo_items:
            # there _are_ existing pkgsinfo items.
            # warn about the problem since we can't seem to read catalogs/all
            print(u'Could not get a list of existing items from the repo: %s'
                  % err)
        return {}
    except CatalogDBException as err:
        # other error while processing catalogs/all
        print (u'Could not get a list of existing items from the repo: %s'
               % err)
        return {}

    if 'installer_item_hash' in pkginfo:
        matchingindexes = catdb['hashes'].get(
            pkginfo['installer_item_hash'])
        if matchingindexes:
            return catdb['items'][matchingindexes[0]]

    if 'receipts' in pkginfo:
        pkgids = [item['packageid']
                  for item in pkginfo['receipts']
                  if 'packageid' in item]
        if pkgids:
            possiblematches = catdb['receipts'].get(pkgids[0])
            if possiblematches:
                versionlist = list(possiblematches.keys())
                versionlist.sort(key=MunkiLooseVersion, reverse=True)
                # go through possible matches, newest version first
                for versionkey in versionlist:
                    testpkgindexes = possiblematches[versionkey]
                    for pkgindex in testpkgindexes:
                        testpkginfo = catdb['items'][pkgindex]
                        testpkgids = [item['packageid'] for item in
                                      testpkginfo.get('receipts', [])
                                      if 'packageid' in item]
                        if set(testpkgids) == set(pkgids):
                            return testpkginfo

    if 'installs' in pkginfo:
        applist = [item for item in pkginfo['installs']
                   if item['type'] == 'application'
                   and 'path' in item]
        if applist:
            app = applist[0]['path']
            possiblematches = catdb['applications'].get(app)
            if possiblematches:
                versionlist = list(possiblematches.keys())
                versionlist.sort(key=MunkiLooseVersion, reverse=True)
                indexes = catdb['applications'][app][versionlist[0]]
                return catdb['items'][indexes[0]]

    if 'PayloadIdentifier' in pkginfo:
        identifier = pkginfo['PayloadIdentifier']
        possiblematches = catdb['profiles'].get(identifier)
        if possiblematches:
            versionlist = list(possiblematches.keys())
            versionlist.sort(key=MunkiLooseVersion, reverse=True)
            indexes = catdb['profiles'][identifier][versionlist[0]]
            return catdb['items'][indexes[0]]

    # no matches by receipts or installed applications,
    # let's try to match based on installer_item_name
    installer_item_name = os.path.basename(
        pkginfo.get('installer_item_location', ''))
    possiblematches = catdb['installer_items'].get(installer_item_name)
    if possiblematches:
        versionlist = list(possiblematches.keys())
        versionlist.sort(key=MunkiLooseVersion, reverse=True)
        indexes = catdb['installer_items'][installer_item_name][versionlist[0]]
        return catdb['items'][indexes[0]]

    # if we get here, we found no matches
    return {}


def make_catalog_db(repo):
    """Returns a dict we can use like a database"""

    try:
        plist = repo.get('catalogs/all')
    except munkirepo.RepoError as err:
        raise CatalogReadException(err) from err

    try:
        catalogitems = plistlib.loads(plist)
    except plistlib.InvalidFileException as err:
        raise CatalogDecodeException(err) from err

    pkgid_table = {}
    app_table = {}
    installer_item_table = {}
    hash_table = {}
    profile_table = {}

    itemindex = -1
    for item in catalogitems:
        itemindex = itemindex + 1
        name = item.get('name', 'NO NAME')
        vers = item.get('version', 'NO VERSION')

        if name == 'NO NAME' or vers == 'NO VERSION':
            print('WARNING: Bad pkginfo: %s' % item, file=sys.stderr)

        # add to hash table
        if 'installer_item_hash' in item:
            if not item['installer_item_hash'] in hash_table:
                hash_table[item['installer_item_hash']] = []
            hash_table[item['installer_item_hash']].append(itemindex)

        # add to installer item table
        if 'installer_item_location' in item:
            installer_item_name = os.path.basename(
                item['installer_item_location'])
            (name, ext) = os.path.splitext(installer_item_name)
            if '-' in name:
                (name, vers) = nameAndVersion(name)
            installer_item_name = name + ext
            if not installer_item_name in installer_item_table:
                installer_item_table[installer_item_name] = {}
            if not vers in installer_item_table[installer_item_name]:
                installer_item_table[installer_item_name][vers] = []
            installer_item_table[installer_item_name][vers].append(itemindex)

        # add to table of receipts
        for receipt in item.get('receipts', []):
            try:
                if 'packageid' in receipt and 'version' in receipt:
                    pkgid = receipt['packageid']
                    pkgvers = receipt['version']
                    if not pkgid in pkgid_table:
                        pkgid_table[pkgid] = {}
                    if not pkgvers in pkgid_table[pkgid]:
                        pkgid_table[pkgid][pkgvers] = []
                    pkgid_table[pkgid][pkgvers].append(itemindex)
            except (TypeError, AttributeError):
                print('Bad receipt data for %s-%s: %s' % (name, vers, receipt),
                      file=sys.stderr)

        # add to table of installed applications
        for install in item.get('installs', []):
            try:
                if install.get('type') == 'application':
                    if 'path' in install:
                        if not install['path'] in app_table:
                            app_table[install['path']] = {}
                        if not vers in app_table[install['path']]:
                            app_table[install['path']][vers] = []
                        app_table[install['path']][vers].append(itemindex)
            except (TypeError, AttributeError):
                print('Bad install data for %s-%s: %s' % (name, vers, install),
                      file=sys.stderr)

        # add to table of PayloadIdentifiers
        if 'PayloadIdentifier' in item:
            if not item['PayloadIdentifier'] in profile_table:
                profile_table[item['PayloadIdentifier']] = {}
            if not vers in profile_table[item['PayloadIdentifier']]:
                profile_table[item['PayloadIdentifier']][vers] = []
            profile_table[item['PayloadIdentifier']][vers].append(itemindex)

    pkgdb = {}
    pkgdb['hashes'] = hash_table
    pkgdb['receipts'] = pkgid_table
    pkgdb['applications'] = app_table
    pkgdb['installer_items'] = installer_item_table
    pkgdb['profiles'] = profile_table
    pkgdb['items'] = catalogitems

    return pkgdb


def copy_pkginfo_to_repo(repo, pkginfo, subdirectory=''):
    """Saves pkginfo to <munki_repo>/pkgsinfo/subdirectory"""
    # less error checking because we copy the installer_item
    # first and bail if it fails...
    destination_path = os.path.join('pkgsinfo', subdirectory)
    pkginfo_ext = pref('pkginfo_extension') or ''
    if pkginfo_ext and not pkginfo_ext.startswith('.'):
        pkginfo_ext = '.' + pkginfo_ext
    arch = determine_arch(pkginfo)
    if arch:
        arch = "-%s" % arch
    pkginfo_name = '%s-%s%s%s' % (pkginfo['name'], pkginfo['version'],
                            arch, pkginfo_ext)
    pkginfo_path = os.path.join(destination_path, pkginfo_name)
    index = 0
    try:
        pkgsinfo_list = list_items_of_kind(repo, 'pkgsinfo')
    except munkirepo.RepoError as err:
        raise RepoCopyError(u'Unable to get list of current pkgsinfo: %s' % err) from err
    while pkginfo_path in pkgsinfo_list:
        index += 1
        pkginfo_name = '%s-%s%s__%s%s' % (pkginfo['name'], pkginfo['version'],
                                        arch, index, pkginfo_ext)
        pkginfo_path = os.path.join(destination_path, pkginfo_name)

    try:
        pkginfo_bytes = plistlib.dumps(pkginfo)
    except plistlib.InvalidFileException as err:
        raise RepoCopyError(err) from err
    try:
        repo.put(pkginfo_path, pkginfo_bytes)
        return pkginfo_path
    except munkirepo.RepoError as err:
        raise RepoCopyError(u'Unable to save pkginfo to %s: %s'
                            % (pkginfo_path, err)) from err


def determine_arch(pkginfo) -> str:
    """Determine a supported architecture string"""
    # If there is exactly one supported architecture, return a string with it
    if len(pkginfo.get("supported_architectures", [])) == 1:
        return pkginfo["supported_architectures"][0]
    return ""


def main():
    """Main routine for Linux-compatible munkiimport"""
    usage = """usage: %prog [options] /path/to/installer_item
       Imports an installer item into a munki repo.
       Installer item can be a .tar.gz package, mobileconfig, or app.
       Example:
       munkiimport --subdirectory apps /path/to/installer_item
       """
    parser = optparse.OptionParser(usage=usage)
    parser.add_option('--subdirectory', default='', help='Subdirectory for uploaded items in the repo.')
    parser.add_option('--nointeractive', '-n', action='store_true', help='No interactive prompts.')
    parser.add_option('--repo_url', '--repo-url', default=pref('repo_url'), help='Specify repo URL.')
    parser.add_option('--plugin', default=pref('plugin'), help='Specify plugin to connect to repo.')
    parser.add_option('--verbose', '-v', action='store_true', help='Verbose output.')
    parser.add_option('--print_warnings', help='Print warnings.', action='store_true')
    parser.add_option('--pkgname', '-p',
        help=('If the installer item is a disk image containing multiple '
              'packages, or the package to be installed is not at the root '
              'of the mounted disk image, PKGNAME is a relative path from '
              'the root of the mounted disk image to the specific package to '
              'be installed.\n'
              'If the installer item is a disk image containing an Adobe '
              'CS4 Deployment Toolkit installation, PKGNAME is the name of '
              'an Adobe CS4 Deployment Toolkit installer package folder at '
              'the top level of the mounted dmg.\n'
              'If this flag is missing, the AdobeUber* files should be at '
              'the top level of the mounted dmg.')
        )
    parser.add_option(
        '--itemname', '-i', '--appname', '-a',
        metavar='ITEM',
        dest='item',
        help=('Name or relative path of the item to be installed. '
              'Useful if there is more than one item at the root of the dmg '
              'or the item is located in a subdirectory. '
              'Absolute paths can be provided as well but they '
              'must point to an item located within the dmg.')
        )
    options, arguments = parser.parse_args()

    if not options.repo_url:
        print('No repo URL found. Please use --repo-url.', file=sys.stderr)
        parser.print_help()
        exit(-1)
    
    installer_item = arguments[0] if arguments else None
    if not installer_item or not os.path.exists(installer_item):
        print('Invalid installer item!', file=sys.stderr)
        exit(-1)
    installer_item = installer_item.rstrip('/')
    
    try:
        repo = munkirepo.connect(options.repo_url, options.plugin)
    except munkirepo.RepoError as err:
        print(f'Could not connect to munki repo: {err}', file=sys.stderr)
        exit(-1)
    
    if os.path.isdir(installer_item):
        print('Directories currently not working', file=sys.stderr)
        exit(-1)

    # make a pkginfo!
    try:
        pkginfo = makepkginfo(installer_item, options)
    except Exception as err:
        # makepkginfo returned an error
        print('Getting package info failed.', file=sys.stderr)
        print(err, file=sys.stderr)
        exit(-1)    


    # try to find existing pkginfo items that match this one
    matchingpkginfo = find_matching_pkginfo(repo, pkginfo)
    exactmatch = False
    if matchingpkginfo:
        if ('installer_item_hash' in matchingpkginfo and
                matchingpkginfo['installer_item_hash'] ==
                pkginfo.get('installer_item_hash')):
            exactmatch = True
            print ('***This item is identical to an existing item in '
                    'the repo***:')
        else:
            print('This item is similar to an existing item in the repo:')
        fields = (('Item name', 'name'),
                    ('Display name', 'display_name'),
                    ('Description', 'description'),
                    ('Version', 'version'),
                    ('Installer item path', 'installer_item_location'))
        for (name, key) in fields:
            print('%21s: %s' % (name, matchingpkginfo.get(key, '')))
        print()
        if exactmatch:
            answer = get_input('Import this item anyway? [y/N] ')
            if not answer.lower().startswith('y'):
                exit(0)

        answer = get_input('Use existing item as a template? [y/N] ')
        if answer.lower().startswith('y'):
            pkginfo['name'] = matchingpkginfo['name']
            pkginfo['display_name'] = (
                matchingpkginfo.get('display_name') or
                pkginfo.get('display_name') or
                matchingpkginfo['name'])
            pkginfo['description'] = pkginfo.get('description') or \
                matchingpkginfo.get('description', '')
            if (options.subdirectory == '' and
                    matchingpkginfo.get('installer_item_location')):
                options.subdirectory = os.path.dirname(
                    matchingpkginfo['installer_item_location'])
            for key in ['blocking_applications',
                        'forced_install',
                        'forced_uninstall',
                        'unattended_install',
                        'unattended_uninstall',
                        'requires',
                        'update_for',
                        'category',
                        'developer',
                        'icon_name',
                        'unused_software_removal_info',
                        'localized_strings',
                        'featured']:
                if key in matchingpkginfo:
                    print('Copying %s: %s' % (key, matchingpkginfo[key]))
                    pkginfo[key] = matchingpkginfo[key]

    # now let user do some basic editing
    editfields = (('Item name', 'name', 'str'),
                    ('Display name', 'display_name', 'str'),
                    ('Description', 'description', 'str'),
                    ('Version', 'version', 'str'),
                    ('Category', 'category', 'str'),
                    ('Developer', 'developer', 'str'),
                    ('Unattended install', 'unattended_install', 'bool'),
                    ('Unattended uninstall', 'unattended_uninstall', 'bool'),
                    )
    for (name, key, kind) in editfields:
        prompt = '%20s: ' % name
        if kind == 'bool':
            default = str(pkginfo.get(key, False))
        else:
            default = pkginfo.get(key, '')
        pkginfo[key] = default
        if kind == 'bool':
            value = pkginfo[key].lower().strip()
            pkginfo[key] = value.startswith(('y', 't'))

    # special handling for catalogs array
    prompt = '%20s: ' % 'Catalogs'
    default = ', '.join(pkginfo['catalogs'])
    newvalue = default
    pkginfo['catalogs'] = [item.strip()
                            for item in newvalue.split(',')]


    if options.subdirectory == '':
        if (isinstance(repo, munkirepo.FileRepo)):
            repo_pkgs_path = os.path.join(repo.root, 'pkgs')
            installer_item_abspath = os.path.abspath(installer_item)
            if installer_item_abspath.startswith(repo_pkgs_path):
                # special case: We're using a file repo and the item being
                # "imported" is actually already in the repo -- we're just
                # creating a pkginfo item and copying it to the repo.
                # In this case, we want to use the same subdirectory for
                # the pkginfo that corresponds to the one the pkg is
                # already in.
                # We aren't handling the case of alternate implementations
                # FileRepo.
                installer_item_dirpath = os.path.dirname(
                    installer_item_abspath)
                options.subdirectory = installer_item_dirpath[
                    len(repo_pkgs_path)+1:]

    # fix in case user accidentally starts subdirectory with a slash
    if options.subdirectory.startswith('/'):
        options.subdirectory = options.subdirectory[1:]


    try:
        print('Copying %s to repo...' % os.path.basename(installer_item))
        uploaded_pkgpath = copy_item_to_repo(
            repo, installer_item, pkginfo.get('version'),
            options.subdirectory)
        print('Copied %s to %s.'
                % (os.path.basename(installer_item), uploaded_pkgpath))
    except RepoCopyError as errmsg:
        print(errmsg, file=sys.stderr)
        exit(-1)

    # adjust the installer_item_location to match
    # the actual location and name
    pkginfo['installer_item_location'] = uploaded_pkgpath.partition('/')[2]

    try:
        pkginfo_path = copy_pkginfo_to_repo(
            repo, pkginfo, options.subdirectory)
        print('Saved pkginfo to %s.' % pkginfo_path)
    except RepoCopyError as errmsg:
        print(errmsg, file=sys.stderr)
        exit(-1)

    print('Import completed successfully!')
    exit(0)