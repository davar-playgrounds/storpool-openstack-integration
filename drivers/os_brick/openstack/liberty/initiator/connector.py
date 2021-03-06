# Copyright 2013 OpenStack Foundation.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
""" Brick Connector objects for each supported transport protocol.

.. module: connector

The connectors here are responsible for discovering and removing volumes for
each of the supported transport protocols.
"""

import abc
import copy
import glob
import json
import os
import platform
import re
import requests
import socket
import sys
import time

from oslo_concurrency import lockutils
from oslo_concurrency import processutils as putils
from oslo_log import log as logging
from oslo_service import loopingcall
from oslo_utils import strutils
import six
from six.moves import urllib

S390X = "s390x"
S390 = "s390"

from os_brick import exception
from os_brick import executor
from os_brick import utils

from os_brick.initiator import host_driver
from os_brick.initiator import linuxfc
from os_brick.initiator import linuxrbd
from os_brick.initiator import linuxscsi
from os_brick.remotefs import remotefs
from os_brick.i18n import _, _LE, _LI, _LW

LOG = logging.getLogger(__name__)

synchronized = lockutils.synchronized_with_prefix('os-brick-')
DEVICE_SCAN_ATTEMPTS_DEFAULT = 3
MULTIPATH_ERROR_REGEX = re.compile("\w{3} \d+ \d\d:\d\d:\d\d \|.*$")
MULTIPATH_DEV_CHECK_REGEX = re.compile("\s+dm-\d+\s+")
MULTIPATH_PATH_CHECK_REGEX = re.compile("\s+\d+:\d+:\d+:\d+\s+")

ISCSI = "ISCSI"
ISER = "ISER"
FIBRE_CHANNEL = "FIBRE_CHANNEL"
AOE = "AOE"
NFS = "NFS"
GLUSTERFS = "GLUSTERFS"
STORPOOL = "STORPOOL"
LOCAL = "LOCAL"
HUAWEISDSHYPERVISOR = "HUAWEISDSHYPERVISOR"
HGST = "HGST"
RBD = "RBD"
SCALEIO = "SCALEIO"
SCALITY = "SCALITY"


def _check_multipathd_running(root_helper, enforce_multipath):
    try:
        putils.execute('multipathd', 'show', 'status',
                       run_as_root=True, root_helper=root_helper)
    except putils.ProcessExecutionError as err:
        LOG.error(_LE('multipathd is not running: exit code %(err)s'),
                  {'err': err.exit_code})
        if enforce_multipath:
            raise
        return False

    return True


def get_connector_properties(root_helper, my_ip, multipath, enforce_multipath,
                             host=None):
    """Get the connection properties for all protocols.

    When the connector wants to use multipath, multipath=True should be
    specified. If enforce_multipath=True is specified too, an exception is
    thrown when multipathd is not running. Otherwise, it falls back to
    multipath=False and only the first path shown up is used.
    For the compatibility reason, even if multipath=False is specified,
    some cinder storage drivers may export the target for multipath, which
    can be found via sendtargets discovery.
    """

    iscsi = ISCSIConnector(root_helper=root_helper)
    fc = linuxfc.LinuxFibreChannel(root_helper=root_helper)

    props = {}
    props['ip'] = my_ip
    props['host'] = host if host else socket.gethostname()
    initiator = iscsi.get_initiator()
    if initiator:
        props['initiator'] = initiator
    wwpns = fc.get_fc_wwpns()
    if wwpns:
        props['wwpns'] = wwpns
    wwnns = fc.get_fc_wwnns()
    if wwnns:
        props['wwnns'] = wwnns
    props['multipath'] = (multipath and
                          _check_multipathd_running(root_helper,
                                                    enforce_multipath))
    props['platform'] = platform.machine()
    props['os_type'] = sys.platform
    return props


@six.add_metaclass(abc.ABCMeta)
class InitiatorConnector(executor.Executor):
    def __init__(self, root_helper, driver=None,
                 execute=putils.execute,
                 device_scan_attempts=DEVICE_SCAN_ATTEMPTS_DEFAULT,
                 *args, **kwargs):
        super(InitiatorConnector, self).__init__(root_helper, execute=execute,
                                                 *args, **kwargs)
        if not driver:
            driver = host_driver.HostDriver()
        self.set_driver(driver)
        self.device_scan_attempts = device_scan_attempts

    def set_driver(self, driver):
        """The driver is used to find used LUNs."""

        self.driver = driver

    @staticmethod
    def factory(protocol, root_helper, driver=None,
                execute=putils.execute, use_multipath=False,
                device_scan_attempts=DEVICE_SCAN_ATTEMPTS_DEFAULT,
                arch=None,
                *args, **kwargs):
        """Build a Connector object based upon protocol and architecture."""

        # We do this instead of assigning it in the definition
        # to help mocking for unit tests
        if arch is None:
            arch = platform.machine()

        LOG.debug("Factory for %(protocol)s on %(arch)s",
                  {'protocol': protocol, 'arch': arch})
        protocol = protocol.upper()
        if protocol in [ISCSI, ISER]:
            # override transport kwarg for requests not comming
            # from the nova LibvirtISERVolumeDriver
            if protocol == ISER:
                kwargs.update({'transport': 'iser'})
            return ISCSIConnector(root_helper=root_helper,
                                  driver=driver,
                                  execute=execute,
                                  use_multipath=use_multipath,
                                  device_scan_attempts=device_scan_attempts,
                                  *args, **kwargs)
        elif protocol == FIBRE_CHANNEL:
            if arch in (S390, S390X):
                return FibreChannelConnectorS390X(
                    root_helper=root_helper,
                    driver=driver,
                    execute=execute,
                    use_multipath=use_multipath,
                    device_scan_attempts=device_scan_attempts,
                    *args, **kwargs)
            else:
                return FibreChannelConnector(
                    root_helper=root_helper,
                    driver=driver,
                    execute=execute,
                    use_multipath=use_multipath,
                    device_scan_attempts=device_scan_attempts,
                    *args, **kwargs)
        elif protocol == AOE:
            return AoEConnector(root_helper=root_helper,
                                driver=driver,
                                execute=execute,
                                device_scan_attempts=device_scan_attempts,
                                *args, **kwargs)
        elif protocol in (NFS, GLUSTERFS, SCALITY):
            return RemoteFsConnector(mount_type=protocol.lower(),
                                     root_helper=root_helper,
                                     driver=driver,
                                     execute=execute,
                                     device_scan_attempts=device_scan_attempts,
                                     *args, **kwargs)
        elif protocol == STORPOOL:
            return StorPoolConnector(driver=driver,
                                     execute=execute,
                                     root_helper=root_helper,
                                     *args, **kwargs)
        elif protocol == LOCAL:
            return LocalConnector(root_helper=root_helper,
                                  driver=driver,
                                  execute=execute,
                                  device_scan_attempts=device_scan_attempts,
                                  *args, **kwargs)
        elif protocol == HUAWEISDSHYPERVISOR:
            return HuaweiStorHyperConnector(
                root_helper=root_helper,
                driver=driver,
                execute=execute,
                device_scan_attempts=device_scan_attempts,
                *args, **kwargs)
        elif protocol == HGST:
            return HGSTConnector(root_helper=root_helper,
                                 driver=driver,
                                 execute=execute,
                                 device_scan_attempts=device_scan_attempts,
                                 *args, **kwargs)
        elif protocol == RBD:
            return RBDConnector(root_helper=root_helper,
                                driver=driver,
                                execute=execute,
                                device_scan_attempts=device_scan_attempts,
                                *args, **kwargs)
        elif protocol == SCALEIO:
            return ScaleIOConnector(
                root_helper=root_helper,
                driver=driver,
                execute=execute,
                device_scan_attempts=device_scan_attempts,
                *args, **kwargs
            )
        else:
            msg = (_("Invalid InitiatorConnector protocol "
                     "specified %(protocol)s") %
                   dict(protocol=protocol))
            raise ValueError(msg)

    def check_valid_device(self, path, run_as_root=True):
        cmd = ('dd', 'if=%(path)s' % {"path": path},
               'of=/dev/null', 'count=1')
        out, info = None, None
        try:
            out, info = self._execute(*cmd, run_as_root=run_as_root,
                                      root_helper=self._root_helper)
        except putils.ProcessExecutionError as e:
            LOG.error(_LE("Failed to access the device on the path "
                          "%(path)s: %(error)s %(info)s."),
                      {"path": path, "error": e.stderr,
                       "info": info})
            return False
        # If the info is none, the path does not exist.
        if info is None:
            return False
        return True

    @abc.abstractmethod
    def connect_volume(self, connection_properties):
        """Connect to a volume.

        The connection_properties describes the information needed by
        the specific protocol to use to make the connection.
        """
        pass

    @abc.abstractmethod
    def disconnect_volume(self, connection_properties, device_info):
        """Disconnect a volume from the local host.

        The connection_properties are the same as from connect_volume.
        The device_info is returned from connect_volume.
        """
        pass


class FakeConnector(InitiatorConnector):

    fake_path = '/dev/vdFAKE'

    def connect_volume(self, connection_properties):
        fake_device_info = {'type': 'fake',
                            'path': self.fake_path}
        return fake_device_info

    def disconnect_volume(self, connection_properties, device_info):
        pass


class ISCSIConnector(InitiatorConnector):
    """Connector class to attach/detach iSCSI volumes."""
    supported_transports = ['be2iscsi', 'bnx2i', 'cxgb3i', 'default',
                            'cxgb4i', 'qla4xxx', 'ocs', 'iser']

    def __init__(self, root_helper, driver=None,
                 execute=putils.execute, use_multipath=False,
                 device_scan_attempts=DEVICE_SCAN_ATTEMPTS_DEFAULT,
                 transport='default', *args, **kwargs):
        self._linuxscsi = linuxscsi.LinuxSCSI(root_helper, execute)
        super(ISCSIConnector, self).__init__(
            root_helper, driver=driver,
            execute=execute,
            device_scan_attempts=device_scan_attempts,
            transport=transport, *args, **kwargs)
        self.use_multipath = use_multipath
        self.transport = self._validate_iface_transport(transport)

    def set_execute(self, execute):
        super(ISCSIConnector, self).set_execute(execute)
        self._linuxscsi.set_execute(execute)

    def _validate_iface_transport(self, transport_iface):
        """Check that given iscsi_iface uses only supported transports

        Accepted transport names for provided iface param are
        be2iscsi, bnx2i, cxgb3i, cxgb4i, default, qla4xxx, ocs or iser.
        Note the difference between transport and iface;
        unlike default(iscsi_tcp)/iser, this is not one and the same for
        offloaded transports, where the default format is
        transport_name.hwaddress
        """
        # Note that default(iscsi_tcp) and iser do not require a separate
        # iface file, just the transport is enough and do not need to be
        # validated. This is not the case for the other entries in
        # supported_transports array.
        if transport_iface in ['default', 'iser']:
            return transport_iface
        # Will return (6) if iscsi_iface file was not found, or (2) if iscsid
        # could not be contacted
        out = self._run_iscsiadm_bare(['-m',
                                       'iface',
                                       '-I',
                                       transport_iface],
                                      check_exit_code=[0, 2, 6])[0] or ""
        LOG.debug("iscsiadm %(iface)s configuration: stdout=%(out)s.",
                  {'iface': transport_iface, 'out': out})
        for data in [line.split() for line in out.splitlines()]:
            if data[0] == 'iface.transport_name':
                if data[2] in self.supported_transports:
                    return transport_iface

        LOG.warn(_LW("No useable transport found for iscsi iface %s. "
                     "Falling back to default transport."),
                 transport_iface)
        return 'default'

    def _get_transport(self):
        return self.transport

    def _iterate_all_targets(self, connection_properties):
        for ip, iqn, lun in self._get_all_targets(connection_properties):
            props = copy.deepcopy(connection_properties)
            props['target_portal'] = ip
            props['target_iqn'] = iqn
            props['target_lun'] = lun
            for key in ('target_portals', 'target_iqns', 'target_luns'):
                props.pop(key, None)
            yield props

    def _get_all_targets(self, connection_properties):
        if all([key in connection_properties for key in ('target_portals',
                                                         'target_iqns',
                                                         'target_luns')]):
            return zip(connection_properties['target_portals'],
                       connection_properties['target_iqns'],
                       connection_properties['target_luns'])

        return [(connection_properties['target_portal'],
                 connection_properties['target_iqn'],
                 connection_properties.get('target_lun', 0))]

    def _discover_iscsi_portals(self, connection_properties):
        if all([key in connection_properties for key in ('target_portals',
                                                         'target_iqns')]):
            # Use targets specified by connection_properties
            return zip(connection_properties['target_portals'],
                       connection_properties['target_iqns'])

        out = None
        if connection_properties.get('discovery_auth_method'):
            try:
                self._run_iscsiadm_update_discoverydb(connection_properties)
            except putils.ProcessExecutionError as exception:
                # iscsiadm returns 6 for "db record not found"
                if exception.exit_code == 6:
                    # Create a new record for this target and update the db
                    self._run_iscsiadm_bare(
                        ['-m', 'discoverydb',
                         '-t', 'sendtargets',
                         '-p', connection_properties['target_portal'],
                         '--op', 'new'],
                        check_exit_code=[0, 255])
                    self._run_iscsiadm_update_discoverydb(
                        connection_properties
                    )
                else:
                    LOG.error(_LE("Unable to find target portal: "
                                  "%(target_portal)s."),
                              {'target_portal': connection_properties[
                                  'target_portal']})
                    raise
            out = self._run_iscsiadm_bare(
                ['-m', 'discoverydb',
                 '-t', 'sendtargets',
                 '-p', connection_properties['target_portal'],
                 '--discover'],
                check_exit_code=[0, 255])[0] or ""
        else:
            out = self._run_iscsiadm_bare(
                ['-m', 'discovery',
                 '-t', 'sendtargets',
                 '-p', connection_properties['target_portal']],
                check_exit_code=[0, 255])[0] or ""

        return self._get_target_portals_from_iscsiadm_output(out)

    def _run_iscsiadm_update_discoverydb(self, connection_properties):
        return self._execute(
            'iscsiadm',
            '-m', 'discoverydb',
            '-t', 'sendtargets',
            '-p', connection_properties['target_portal'],
            '--op', 'update',
            '-n', "discovery.sendtargets.auth.authmethod",
            '-v', connection_properties['discovery_auth_method'],
            '-n', "discovery.sendtargets.auth.username",
            '-v', connection_properties['discovery_auth_username'],
            '-n', "discovery.sendtargets.auth.password",
            '-v', connection_properties['discovery_auth_password'],
            run_as_root=True,
            root_helper=self._root_helper)

    @synchronized('connect_volume')
    def connect_volume(self, connection_properties):
        """Attach the volume to instance_name.

        connection_properties for iSCSI must include:
        target_portal(s) - ip and optional port
        target_iqn(s) - iSCSI Qualified Name
        target_lun(s) - LUN id of the volume
        Note that plural keys may be used when use_multipath=True
        """

        device_info = {'type': 'block'}

        if self.use_multipath:
            # Multipath installed, discovering other targets if available
            try:
                ips_iqns = self._discover_iscsi_portals(connection_properties)
            except Exception:
                raise exception.TargetPortalNotFound(
                    target_portal=connection_properties['target_portal'])

            if not connection_properties.get('target_iqns'):
                # There are two types of iSCSI multipath devices. One which
                # shares the same iqn between multiple portals, and the other
                # which use different iqns on different portals.
                # Try to identify the type by checking the iscsiadm output
                # if the iqn is used by multiple portals. If it is, it's
                # the former, so use the supplied iqn. Otherwise, it's the
                # latter, so try the ip,iqn combinations to find the targets
                # which constitutes the multipath device.
                main_iqn = connection_properties['target_iqn']
                all_portals = set([ip for ip, iqn in ips_iqns])
                match_portals = set([ip for ip, iqn in ips_iqns
                                     if iqn == main_iqn])
                if len(all_portals) == len(match_portals):
                    ips_iqns = zip(all_portals, [main_iqn] * len(all_portals))

            for ip, iqn in ips_iqns:
                props = copy.deepcopy(connection_properties)
                props['target_portal'] = ip
                props['target_iqn'] = iqn
                self._connect_to_iscsi_portal(props)

            self._rescan_iscsi()
            host_devices = self._get_device_path(connection_properties)
        else:
            target_props = connection_properties
            for props in self._iterate_all_targets(connection_properties):
                if self._connect_to_iscsi_portal(props):
                    target_props = props
                    break
                else:
                    LOG.warning(_LW(
                        'Failed to connect to iSCSI portal %(portal)s.'),
                        {'portal': props['target_portal']})

            host_devices = self._get_device_path(target_props)

        # The /dev/disk/by-path/... node is not always present immediately
        # TODO(justinsb): This retry-with-delay is a pattern, move to utils?
        tries = 0
        # Loop until at least 1 path becomes available
        while all(map(lambda x: not os.path.exists(x), host_devices)):
            if tries >= self.device_scan_attempts:
                raise exception.VolumeDeviceNotFound(device=host_devices)

            LOG.warning(_LW("ISCSI volume not yet found at: %(host_devices)s. "
                            "Will rescan & retry.  Try number: %(tries)s."),
                        {'host_devices': host_devices,
                         'tries': tries})

            # The rescan isn't documented as being necessary(?), but it helps
            if self.use_multipath:
                self._rescan_iscsi()
            else:
                if (tries):
                    host_devices = self._get_device_path(target_props)
                self._run_iscsiadm(target_props, ("--rescan",))

            tries = tries + 1
            if all(map(lambda x: not os.path.exists(x), host_devices)):
                time.sleep(tries ** 2)
            else:
                break

        if tries != 0:
            LOG.debug("Found iSCSI node %(host_devices)s "
                      "(after %(tries)s rescans)",
                      {'host_devices': host_devices, 'tries': tries})

        # Choose an accessible host device
        host_device = next(dev for dev in host_devices if os.path.exists(dev))

        if self.use_multipath:
            # We use the multipath device instead of the single path device
            self._rescan_multipath()
            multipath_device = self._get_multipath_device_name(host_device)
            if multipath_device is not None:
                host_device = multipath_device
                LOG.debug("Unable to find multipath device name for "
                          "volume. Only using path %(device)s "
                          "for volume.", {'device': host_device})

        device_info['path'] = host_device
        return device_info

    @synchronized('connect_volume')
    def disconnect_volume(self, connection_properties, device_info):
        """Detach the volume from instance_name.

        connection_properties for iSCSI must include:
        target_portal(s) - IP and optional port
        target_iqn(s) - iSCSI Qualified Name
        target_lun(s) - LUN id of the volume
        """
        if self.use_multipath:
            self._rescan_multipath()
            host_device = multipath_device = None
            host_devices = self._get_device_path(connection_properties)
            # Choose an accessible host device
            for dev in host_devices:
                if os.path.exists(dev):
                    host_device = dev
                    multipath_device = self._get_multipath_device_name(dev)
                    if multipath_device:
                        break
            if not host_device:
                LOG.error(_LE("No accessible volume device: %(host_devices)s"),
                          {'host_devices': host_devices})
                raise exception.VolumeDeviceNotFound(device=host_devices)

            if multipath_device:
                device_realpath = os.path.realpath(host_device)
                self._linuxscsi.remove_multipath_device(device_realpath)
                return self._disconnect_volume_multipath_iscsi(
                    connection_properties, multipath_device)

        # When multiple portals/iqns/luns are specified, we need to remove
        # unused devices created by logging into other LUNs' session.
        for props in self._iterate_all_targets(connection_properties):
            self._disconnect_volume_iscsi(props)

    def _disconnect_volume_iscsi(self, connection_properties):
        # remove the device from the scsi subsystem
        # this eliminates any stale entries until logout
        host_devices = self._get_device_path(connection_properties)

        if host_devices:
            host_device = host_devices[0]
        else:
            return

        dev_name = self._linuxscsi.get_name_from_path(host_device)
        if dev_name:
            self._linuxscsi.remove_scsi_device(dev_name)

            # NOTE(jdg): On busy systems we can have a race here
            # where remove_iscsi_device is called before the device file
            # has actually been removed.   The result is an orphaned
            # iscsi session that never gets logged out.  The following
            # call to wait addresses that issue.
            self._linuxscsi.wait_for_volume_removal(host_device)

        # NOTE(vish): Only disconnect from the target if no luns from the
        #             target are in use.
        device_byname = ("ip-%(portal)s-iscsi-%(iqn)s-lun-" %
                         {'portal': connection_properties['target_portal'],
                          'iqn': connection_properties['target_iqn']})
        devices = self.driver.get_all_block_devices()
        devices = [dev for dev in devices if (device_byname in dev
                                              and
                                              dev.startswith(
                                                  '/dev/disk/by-path/'))
                   and os.path.exists(dev)]
        if not devices:
            self._disconnect_from_iscsi_portal(connection_properties)

    def _get_device_path(self, connection_properties):
        if self._get_transport() == "default":
            return ["/dev/disk/by-path/ip-%s-iscsi-%s-lun-%s" % x for x in
                    self._get_all_targets(connection_properties)]
        else:
            # we are looking for paths in the format :
            # /dev/disk/by-path/pci-XXXX:XX:XX.X-ip-PORTAL:PORT-iscsi-IQN-lun-LUN_ID
            device_list = []
            for x in self._get_all_targets(connection_properties):
                look_for_device = glob.glob('/dev/disk/by-path/*ip-%s-iscsi-%s-lun-%s' % x)  # noqa
                if look_for_device:
                    device_list.extend(look_for_device)
            return device_list

    def get_initiator(self):
        """Secure helper to read file as root."""
        file_path = '/etc/iscsi/initiatorname.iscsi'
        try:
            lines, _err = self._execute('cat', file_path, run_as_root=True,
                                        root_helper=self._root_helper)

            for l in lines.split('\n'):
                if l.startswith('InitiatorName='):
                    return l[l.index('=') + 1:].strip()
        except putils.ProcessExecutionError:
            LOG.warning(_LW("Could not find the iSCSI Initiator File %s"),
                        file_path)
            return None

    def _run_iscsiadm(self, connection_properties, iscsi_command, **kwargs):
        check_exit_code = kwargs.pop('check_exit_code', 0)
        attempts = kwargs.pop('attempts', 1)
        delay_on_retry = kwargs.pop('delay_on_retry', True)
        (out, err) = self._execute('iscsiadm', '-m', 'node', '-T',
                                   connection_properties['target_iqn'],
                                   '-p',
                                   connection_properties['target_portal'],
                                   *iscsi_command, run_as_root=True,
                                   root_helper=self._root_helper,
                                   check_exit_code=check_exit_code,
                                   attempts=attempts,
                                   delay_on_retry=delay_on_retry)
        msg = ("iscsiadm %(iscsi_command)s: stdout=%(out)s stderr=%(err)s",
               {'iscsi_command': iscsi_command, 'out': out, 'err': err})
        # don't let passwords be shown in log output
        LOG.debug(strutils.mask_password(msg))

        return (out, err)

    def _iscsiadm_update(self, connection_properties, property_key,
                         property_value, **kwargs):
        iscsi_command = ('--op', 'update', '-n', property_key,
                         '-v', property_value)
        return self._run_iscsiadm(connection_properties, iscsi_command,
                                  **kwargs)

    def _get_target_portals_from_iscsiadm_output(self, output):
        # return both portals and iqns
        #
        # as we are parsing a command line utility, allow for the
        # possibility that additional debug data is spewed in the
        # stream, and only grab actual ip / iqn lines.
        targets = []
        for data in [line.split() for line in output.splitlines()]:
            if len(data) == 2 and data[1].startswith('iqn.'):
                targets.append(data)
        return targets

    def _disconnect_volume_multipath_iscsi(self, connection_properties,
                                           multipath_name):
        """This removes a multipath device and it's LUNs."""
        LOG.debug("Disconnect multipath device %s", multipath_name)
        mpath_map = self._get_multipath_device_map()
        block_devices = self.driver.get_all_block_devices()
        devices = []
        for dev in block_devices:
            if os.path.exists(dev):
                if "/mapper/" in dev:
                    devices.append(dev)
                else:
                    mpdev = mpath_map.get(dev)
                    if mpdev:
                        devices.append(mpdev)

        # Do a discovery to find all targets.
        # Targets for multiple paths for the same multipath device
        # may not be the same.
        all_ips_iqns = self._discover_iscsi_portals(connection_properties)

        # As discovery result may contain other targets' iqns, extract targets
        # to be disconnected whose block devices are already deleted here.
        ips_iqns = []
        entries = [device.lstrip('ip-').split('-lun-')[0]
                   for device in self._get_iscsi_devices()]
        for ip, iqn in all_ips_iqns:
            ip_iqn = "%s-iscsi-%s" % (ip.split(",")[0], iqn)
            if ip_iqn not in entries:
                ips_iqns.append([ip, iqn])

        if not devices:
            # disconnect if no other multipath devices
            self._disconnect_mpath(connection_properties, ips_iqns)
            return

        # Get a target for all other multipath devices
        other_iqns = self._get_multipath_iqns(devices, mpath_map)

        # Get all the targets for the current multipath device
        current_iqns = [iqn for ip, iqn in ips_iqns]

        in_use = False
        for current in current_iqns:
            if current in other_iqns:
                in_use = True
                break

        # If no other multipath device attached has the same iqn
        # as the current device
        if not in_use:
            # disconnect if no other multipath devices with same iqn
            self._disconnect_mpath(connection_properties, ips_iqns)
            return

        # else do not disconnect iscsi portals,
        # as they are used for other luns
        return

    def _connect_to_iscsi_portal(self, connection_properties):
        # NOTE(vish): If we are on the same host as nova volume, the
        #             discovery makes the target so we don't need to
        #             run --op new. Therefore, we check to see if the
        #             target exists, and if we get 255 (Not Found), then
        #             we run --op new. This will also happen if another
        #             volume is using the same target.
        try:
            self._run_iscsiadm(connection_properties, ())
        except putils.ProcessExecutionError as exc:
            # iscsiadm returns 21 for "No records found" after version 2.0-871
            if exc.exit_code in [21, 255]:
                self._run_iscsiadm(connection_properties,
                                   ('--interface', self._get_transport(),
                                    '--op', 'new'))
            else:
                raise

        if connection_properties.get('auth_method'):
            self._iscsiadm_update(connection_properties,
                                  "node.session.auth.authmethod",
                                  connection_properties['auth_method'])
            self._iscsiadm_update(connection_properties,
                                  "node.session.auth.username",
                                  connection_properties['auth_username'])
            self._iscsiadm_update(connection_properties,
                                  "node.session.auth.password",
                                  connection_properties['auth_password'])

        # Duplicate logins crash iscsiadm after load,
        # so we scan active sessions to see if the node is logged in.
        out = self._run_iscsiadm_bare(["-m", "session"],
                                      run_as_root=True,
                                      check_exit_code=[0, 1, 21])[0] or ""

        portals = [{'portal': p.split(" ")[2], 'iqn': p.split(" ")[3]}
                   for p in out.splitlines() if p.startswith("tcp:")]

        stripped_portal = connection_properties['target_portal'].split(",")[0]
        if len(portals) == 0 or len([s for s in portals
                                     if stripped_portal ==
                                     s['portal'].split(",")[0]
                                     and
                                     s['iqn'] ==
                                     connection_properties['target_iqn']]
                                    ) == 0:
            try:
                self._run_iscsiadm(connection_properties,
                                   ("--login",),
                                   check_exit_code=[0, 255])
            except putils.ProcessExecutionError as err:
                # exit_code=15 means the session already exists, so it should
                # be regarded as successful login.
                if err.exit_code not in [15]:
                    LOG.warning(_LW('Failed to login iSCSI target %(iqn)s '
                                    'on portal %(portal)s (exit code '
                                    '%(err)s).'),
                                {'iqn': connection_properties['target_iqn'],
                                 'portal': connection_properties[
                                     'target_portal'],
                                 'err': err.exit_code})
                    return False

            self._iscsiadm_update(connection_properties,
                                  "node.startup",
                                  "automatic")
        return True

    def _disconnect_from_iscsi_portal(self, connection_properties):
        self._iscsiadm_update(connection_properties, "node.startup", "manual",
                              check_exit_code=[0, 21, 255])
        self._run_iscsiadm(connection_properties, ("--logout",),
                           check_exit_code=[0, 21, 255])
        self._run_iscsiadm(connection_properties, ('--op', 'delete'),
                           check_exit_code=[0, 21, 255],
                           attempts=5,
                           delay_on_retry=True)

    def _get_multipath_device_name(self, single_path_device):
        device = os.path.realpath(single_path_device)
        out = self._run_multipath(['-ll',
                                  device],
                                  check_exit_code=[0, 1])[0]
        mpath_line = [line for line in out.splitlines()
                      if not re.match(MULTIPATH_ERROR_REGEX, line)]
        if len(mpath_line) > 0 and len(mpath_line[0]) > 0:
            return "/dev/mapper/%s" % mpath_line[0].split(" ")[0]

        return None

    def _get_iscsi_devices(self):
        try:
            devices = list(os.walk('/dev/disk/by-path'))[0][-1]
        except IndexError:
            return []
        # For iSCSI HBAs, look at an offset of len('pci-0000:00:00.0')
        return [entry for entry in devices if (entry.startswith("ip-")
                                               or (entry.startswith("pci-")
                                                   and
                                                   entry.find("ip-", 16, 21)
                                                   >= 16))]

    def _disconnect_mpath(self, connection_properties, ips_iqns):
        for ip, iqn in ips_iqns:
            props = copy.deepcopy(connection_properties)
            props['target_portal'] = ip
            props['target_iqn'] = iqn
            self._disconnect_from_iscsi_portal(props)

        self._rescan_multipath()

    def _get_multipath_iqns(self, multipath_devices, mpath_map):
        entries = self._get_iscsi_devices()
        iqns = []
        for entry in entries:
            entry_real_path = os.path.realpath("/dev/disk/by-path/%s" % entry)
            entry_multipath = mpath_map.get(entry_real_path)
            if entry_multipath and entry_multipath in multipath_devices:
                iqns.append(entry.split("iscsi-")[1].split("-lun")[0])
        return iqns

    def _get_multipath_device_map(self):
        out = self._run_multipath(['-ll'], check_exit_code=[0, 1])[0]
        mpath_line = [line for line in out.splitlines()
                      if not re.match(MULTIPATH_ERROR_REGEX, line)]
        mpath_dev = None
        mpath_map = {}
        for line in out.splitlines():
            m = MULTIPATH_DEV_CHECK_REGEX.split(line)
            if len(m) >= 2:
                mpath_dev = '/dev/mapper/' + m[0].split(" ")[0]
                continue
            m = MULTIPATH_PATH_CHECK_REGEX.split(line)
            if len(m) >= 2:
                mpath_map['/dev/' + m[1].split(" ")[0]] = mpath_dev

        if mpath_line and not mpath_map:
            LOG.warn(_LW("Failed to parse the output of multipath -ll."))
        return mpath_map

    def _run_iscsiadm_bare(self, iscsi_command, **kwargs):
        check_exit_code = kwargs.pop('check_exit_code', 0)
        (out, err) = self._execute('iscsiadm',
                                   *iscsi_command,
                                   run_as_root=True,
                                   root_helper=self._root_helper,
                                   check_exit_code=check_exit_code)
        LOG.debug("iscsiadm %(iscsi_command)s: stdout=%(out)s stderr=%(err)s",
                  {'iscsi_command': iscsi_command, 'out': out, 'err': err})
        return (out, err)

    def _run_multipath(self, multipath_command, **kwargs):
        check_exit_code = kwargs.pop('check_exit_code', 0)
        (out, err) = self._execute('multipath',
                                   *multipath_command,
                                   run_as_root=True,
                                   root_helper=self._root_helper,
                                   check_exit_code=check_exit_code)
        LOG.debug("multipath %(multipath_command)s: "
                  "stdout=%(out)s stderr=%(err)s",
                  {'multipath_command': multipath_command,
                   'out': out, 'err': err})
        return (out, err)

    def _rescan_iscsi(self):
        self._run_iscsiadm_bare(('-m', 'node', '--rescan'),
                                check_exit_code=[0, 1, 21, 255])
        self._run_iscsiadm_bare(('-m', 'session', '--rescan'),
                                check_exit_code=[0, 1, 21, 255])

    def _rescan_multipath(self):
        self._run_multipath(['-r'], check_exit_code=[0, 1, 21])


class FibreChannelConnector(InitiatorConnector):
    """Connector class to attach/detach Fibre Channel volumes."""

    def __init__(self, root_helper, driver=None,
                 execute=putils.execute, use_multipath=False,
                 device_scan_attempts=DEVICE_SCAN_ATTEMPTS_DEFAULT,
                 *args, **kwargs):
        self._linuxscsi = linuxscsi.LinuxSCSI(root_helper, execute)
        self._linuxfc = linuxfc.LinuxFibreChannel(root_helper, execute)
        super(FibreChannelConnector, self).__init__(
            root_helper, driver=driver,
            execute=execute,
            device_scan_attempts=device_scan_attempts,
            *args, **kwargs)
        self.use_multipath = use_multipath

    def set_execute(self, execute):
        super(FibreChannelConnector, self).set_execute(execute)
        self._linuxscsi.set_execute(execute)
        self._linuxfc.set_execute(execute)

    def _get_possible_volume_paths(self, connection_properties, hbas):
        ports = connection_properties['target_wwn']
        possible_devs = self._get_possible_devices(hbas, ports)

        lun = connection_properties.get('target_lun', 0)
        host_paths = self._get_host_devices(possible_devs, lun)
        return host_paths

    def _get_volume_paths(self, connection_properties):
        """Get a list of existing paths for a volume on the system."""
        volume_paths = []
        # first fetch all of the potential paths that might exist
        # and then filter those by what's actually on the system.
        hbas = self._linuxfc.get_fc_hbas_info()
        host_paths = self._get_possible_volume_paths(connection_properties,
                                                     hbas)
        for path in host_paths:
            if os.path.exists(path):
                volume_paths.append(path)

        return volume_paths

    @synchronized('connect_volume')
    def connect_volume(self, connection_properties):
        """Attach the volume to instance_name.

        connection_properties for Fibre Channel must include:
        target_wwn - World Wide Name
        target_lun - LUN id of the volume
        """
        LOG.debug("execute = %s", self._execute)
        device_info = {'type': 'block'}

        hbas = self._linuxfc.get_fc_hbas_info()
        host_devices = self._get_possible_volume_paths(connection_properties,
                                                       hbas)

        if len(host_devices) == 0:
            # this is empty because we don't have any FC HBAs
            LOG.warning(
                _LW("We are unable to locate any Fibre Channel devices"))
            raise exception.NoFibreChannelHostsFound()

        # The /dev/disk/by-path/... node is not always present immediately
        # We only need to find the first device.  Once we see the first device
        # multipath will have any others.
        def _wait_for_device_discovery(host_devices):
            tries = self.tries
            for device in host_devices:
                LOG.debug("Looking for Fibre Channel dev %(device)s",
                          {'device': device})
                if os.path.exists(device):
                    self.host_device = device
                    # get the /dev/sdX device.  This is used
                    # to find the multipath device.
                    self.device_name = os.path.realpath(device)
                    raise loopingcall.LoopingCallDone()

            if self.tries >= self.device_scan_attempts:
                LOG.error(_LE("Fibre Channel volume device not found."))
                raise exception.NoFibreChannelVolumeDeviceFound()

            LOG.warning(_LW("Fibre Channel volume device not yet found. "
                            "Will rescan & retry.  Try number: %(tries)s."),
                        {'tries': tries})

            self._linuxfc.rescan_hosts(hbas)
            self.tries = self.tries + 1

        self.host_device = None
        self.device_name = None
        self.tries = 0
        timer = loopingcall.FixedIntervalLoopingCall(
            _wait_for_device_discovery, host_devices)
        timer.start(interval=2).wait()

        tries = self.tries
        if self.host_device is not None and self.device_name is not None:
            LOG.debug("Found Fibre Channel volume %(name)s "
                      "(after %(tries)s rescans)",
                      {'name': self.device_name, 'tries': tries})

        # find out the WWN of the device
        device_wwn = self._linuxscsi.get_scsi_wwn(self.host_device)
        LOG.debug("Device WWN = '%(wwn)s'", {'wwn': device_wwn})

        # see if the new drive is part of a multipath
        # device.  If so, we'll use the multipath device.
        if self.use_multipath:

            path = self._linuxscsi.find_multipath_device_path(device_wwn)
            if path is not None:
                LOG.debug("Multipath device path discovered %(device)s",
                          {'device': path})
                device_path = path
                # for temporary backwards compatibility
                device_info['multipath_id'] = device_wwn
            else:
                mpath_info = self._linuxscsi.find_multipath_device(
                    self.device_name)
                if mpath_info:
                    device_path = mpath_info['device']
                    # for temporary backwards compatibility
                    device_info['multipath_id'] = device_wwn
                else:
                    # we didn't find a multipath device.
                    # so we assume the kernel only sees 1 device
                    device_path = self.host_device
                    LOG.debug("Unable to find multipath device name for "
                              "volume. Using path %(device)s for volume.",
                              {'device': self.host_device})

            if connection_properties.get('access_mode', '') != 'ro':
                try:
                    # Sometimes the multipath devices will show up as read only
                    # initially and need additional time/rescans to get to RW.
                    self._linuxscsi.wait_for_rw(device_wwn, device_path)
                except exception.BlockDeviceReadOnly:
                    LOG.warning(_LW('Block device %s is still read-only. '
                                    'Continuing anyway.'), device_path)

        else:
            device_path = self.host_device

        device_info['path'] = device_path
        return device_info

    def _get_host_devices(self, possible_devs, lun):
        host_devices = []
        for pci_num, target_wwn in possible_devs:
            host_device = "/dev/disk/by-path/pci-%s-fc-%s-lun-%s" % (
                pci_num,
                target_wwn,
                lun)
            host_devices.append(host_device)
        return host_devices

    def _get_possible_devices(self, hbas, wwnports):
        """Compute the possible valid fibre channel device options.

        :param hbas: available hba devices.
        :param wwnports: possible wwn addresses. Can either be string
        or list of strings.

        :returns: list of (pci_id, wwn) tuples

        Given one or more wwn (mac addresses for fibre channel) ports
        do the matrix math to figure out a set of pci device, wwn
        tuples that are potentially valid (they won't all be). This
        provides a search space for the device connection.

        """
        # the wwn (think mac addresses for fiber channel devices) can
        # either be a single value or a list. Normalize it to a list
        # for further operations.
        wwns = []
        if isinstance(wwnports, list):
            for wwn in wwnports:
                wwns.append(str(wwn))
        elif isinstance(wwnports, six.string_types):
            wwns.append(str(wwnports))

        raw_devices = []
        for hba in hbas:
            pci_num = self._get_pci_num(hba)
            if pci_num is not None:
                for wwn in wwns:
                    target_wwn = "0x%s" % wwn.lower()
                    raw_devices.append((pci_num, target_wwn))
        return raw_devices

    @synchronized('connect_volume')
    def disconnect_volume(self, connection_properties, device_info):
        """Detach the volume from instance_name.

        connection_properties for Fibre Channel must include:
        target_wwn - World Wide Name
        target_lun - LUN id of the volume
        """

        devices = []
        volume_paths = self._get_volume_paths(connection_properties)
        wwn = None
        for path in volume_paths:
            real_path = self._linuxscsi.get_name_from_path(path)
            if not wwn:
                wwn = self._linuxscsi.get_scsi_wwn(path)
            device_info = self._linuxscsi.get_device_info(real_path)
            devices.append(device_info)

        LOG.debug("devices to remove = %s", devices)
        self._remove_devices(connection_properties, devices)

        if self.use_multipath:
            # There is a bug in multipath where the flushing
            # doesn't remove the entry if friendly names are on
            # we'll try anyway.
            self._linuxscsi.flush_multipath_device(wwn)

    def _remove_devices(self, connection_properties, devices):
        # There may have been more than 1 device mounted
        # by the kernel for this volume.  We have to remove
        # all of them
        for device in devices:
            self._linuxscsi.remove_scsi_device(device["device"])

    def _get_pci_num(self, hba):
        # NOTE(walter-boring)
        # device path is in format of (FC and FCoE) :
        # /sys/devices/pci0000:00/0000:00:03.0/0000:05:00.3/host2/fc_host/host2
        # /sys/devices/pci0000:20/0000:20:03.0/0000:21:00.2/net/ens2f2/ctlr_2
        # /host3/fc_host/host3
        # we always want the value prior to the host or net value
        if hba is not None:
            if "device_path" in hba:
                device_path = hba['device_path'].split('/')
                for index, value in enumerate(device_path):
                    if value.startswith('net') or value.startswith('host'):
                        return device_path[index - 1]
        return None


class FibreChannelConnectorS390X(FibreChannelConnector):
    """Connector class to attach/detach Fibre Channel volumes on S390X arch."""

    def __init__(self, root_helper, driver=None,
                 execute=putils.execute, use_multipath=False,
                 device_scan_attempts=DEVICE_SCAN_ATTEMPTS_DEFAULT,
                 *args, **kwargs):
        super(FibreChannelConnectorS390X, self).__init__(
            root_helper,
            driver=driver,
            execute=execute,
            device_scan_attempts=device_scan_attempts,
            *args, **kwargs)
        LOG.debug("Initializing Fibre Channel connector for S390")
        self._linuxscsi = linuxscsi.LinuxSCSI(root_helper, execute)
        self._linuxfc = linuxfc.LinuxFibreChannelS390X(root_helper, execute)
        self.use_multipath = use_multipath

    def set_execute(self, execute):
        super(FibreChannelConnectorS390X, self).set_execute(execute)
        self._linuxscsi.set_execute(execute)
        self._linuxfc.set_execute(execute)

    def _get_host_devices(self, possible_devs, lun):
        host_devices = []
        for pci_num, target_wwn in possible_devs:
            target_lun = self._get_lun_string(lun)
            host_device = self._get_device_file_path(
                pci_num,
                target_wwn,
                target_lun)
            self._linuxfc.configure_scsi_device(pci_num, target_wwn,
                                                target_lun)
            host_devices.append(host_device)
        return host_devices

    def _get_lun_string(self, lun):
        target_lun = 0
        if lun <= 0xffff:
            target_lun = "0x%04x000000000000" % lun
        elif lun <= 0xffffffff:
            target_lun = "0x%08x00000000" % lun
        return target_lun

    def _get_device_file_path(self, pci_num, target_wwn, target_lun):
        host_device = "/dev/disk/by-path/ccw-%s-zfcp-%s:%s" % (
            pci_num,
            target_wwn,
            target_lun)
        return host_device

    def _remove_devices(self, connection_properties, devices):
        hbas = self._linuxfc.get_fc_hbas_info()
        ports = connection_properties['target_wwn']
        possible_devs = self._get_possible_devices(hbas, ports)
        lun = connection_properties.get('target_lun', 0)
        target_lun = self._get_lun_string(lun)
        for pci_num, target_wwn in possible_devs:
            self._linuxfc.deconfigure_scsi_device(pci_num,
                                                  target_wwn,
                                                  target_lun)


class AoEConnector(InitiatorConnector):
    """Connector class to attach/detach AoE volumes."""
    def __init__(self, root_helper, driver=None,
                 execute=putils.execute,
                 device_scan_attempts=DEVICE_SCAN_ATTEMPTS_DEFAULT,
                 *args, **kwargs):
        super(AoEConnector, self).__init__(
            root_helper,
            driver=driver,
            execute=execute,
            device_scan_attempts=device_scan_attempts,
            *args, **kwargs)

    def _get_aoe_info(self, connection_properties):
        shelf = connection_properties['target_shelf']
        lun = connection_properties['target_lun']
        aoe_device = 'e%(shelf)s.%(lun)s' % {'shelf': shelf,
                                             'lun': lun}
        aoe_path = '/dev/etherd/%s' % (aoe_device)
        return aoe_device, aoe_path

    @lockutils.synchronized('aoe_control', 'aoe-')
    def connect_volume(self, connection_properties):
        """Discover and attach the volume.

        connection_properties for AoE must include:
        target_shelf - shelf id of volume
        target_lun - lun id of volume
        """
        aoe_device, aoe_path = self._get_aoe_info(connection_properties)

        device_info = {
            'type': 'block',
            'device': aoe_device,
            'path': aoe_path,
        }

        if os.path.exists(aoe_path):
            self._aoe_revalidate(aoe_device)
        else:
            self._aoe_discover()

        waiting_status = {'tries': 0}

        # NOTE(jbr_): Device path is not always present immediately
        def _wait_for_discovery(aoe_path):
            if os.path.exists(aoe_path):
                raise loopingcall.LoopingCallDone

            if waiting_status['tries'] >= self.device_scan_attempts:
                raise exception.VolumeDeviceNotFound(device=aoe_path)

            LOG.warning(_LW("AoE volume not yet found at: %(path)s. "
                            "Try number: %(tries)s"),
                        {'path': aoe_device,
                         'tries': waiting_status['tries']})

            self._aoe_discover()
            waiting_status['tries'] += 1

        timer = loopingcall.FixedIntervalLoopingCall(_wait_for_discovery,
                                                     aoe_path)
        timer.start(interval=2).wait()

        if waiting_status['tries']:
            LOG.debug("Found AoE device %(path)s "
                      "(after %(tries)s rediscover)",
                      {'path': aoe_path,
                       'tries': waiting_status['tries']})

        return device_info

    @lockutils.synchronized('aoe_control', 'aoe-')
    def disconnect_volume(self, connection_properties, device_info):
        """Detach and flush the volume.

        connection_properties for AoE must include:
        target_shelf - shelf id of volume
        target_lun - lun id of volume
        """
        aoe_device, aoe_path = self._get_aoe_info(connection_properties)

        if os.path.exists(aoe_path):
            self._aoe_flush(aoe_device)

    def _aoe_discover(self):
        (out, err) = self._execute('aoe-discover',
                                   run_as_root=True,
                                   root_helper=self._root_helper,
                                   check_exit_code=0)

        LOG.debug('aoe-discover: stdout=%(out)s stderr%(err)s',
                  {'out': out, 'err': err})

    def _aoe_revalidate(self, aoe_device):
        (out, err) = self._execute('aoe-revalidate',
                                   aoe_device,
                                   run_as_root=True,
                                   root_helper=self._root_helper,
                                   check_exit_code=0)

        LOG.debug('aoe-revalidate %(dev)s: stdout=%(out)s stderr%(err)s',
                  {'dev': aoe_device, 'out': out, 'err': err})

    def _aoe_flush(self, aoe_device):
        (out, err) = self._execute('aoe-flush',
                                   aoe_device,
                                   run_as_root=True,
                                   root_helper=self._root_helper,
                                   check_exit_code=0)
        LOG.debug('aoe-flush %(dev)s: stdout=%(out)s stderr%(err)s',
                  {'dev': aoe_device, 'out': out, 'err': err})


class RemoteFsConnector(InitiatorConnector):
    """Connector class to attach/detach NFS and GlusterFS volumes."""

    def __init__(self, mount_type, root_helper, driver=None,
                 execute=putils.execute,
                 device_scan_attempts=DEVICE_SCAN_ATTEMPTS_DEFAULT,
                 *args, **kwargs):
        kwargs = kwargs or {}
        conn = kwargs.get('conn')
        if conn:
            mount_point_base = conn.get('mount_point_base')
            mount_type_lower = mount_type.lower()
            if mount_type_lower in ('nfs', 'glusterfs', 'scality'):
                kwargs[mount_type_lower + '_mount_point_base'] = (
                    kwargs.get(mount_type_lower + '_mount_point_base') or
                    mount_point_base)
        else:
            LOG.warning(_LW("Connection details not present."
                            " RemoteFsClient may not initialize properly."))
        self._remotefsclient = remotefs.RemoteFsClient(mount_type, root_helper,
                                                       execute=execute,
                                                       *args, **kwargs)
        super(RemoteFsConnector, self).__init__(
            root_helper, driver=driver,
            execute=execute,
            device_scan_attempts=device_scan_attempts,
            *args, **kwargs)

    def set_execute(self, execute):
        super(RemoteFsConnector, self).set_execute(execute)
        self._remotefsclient.set_execute(execute)

    def connect_volume(self, connection_properties):
        """Ensure that the filesystem containing the volume is mounted.

        connection_properties must include:
        export - remote filesystem device (e.g. '172.18.194.100:/var/nfs')
        name - file name within the filesystem

        connection_properties may optionally include:
        options - options to pass to mount
        """

        mnt_flags = []
        if connection_properties.get('options'):
            mnt_flags = connection_properties['options'].split()

        nfs_share = connection_properties['export']
        self._remotefsclient.mount(nfs_share, mnt_flags)
        mount_point = self._remotefsclient.get_mount_point(nfs_share)

        path = mount_point + '/' + connection_properties['name']

        return {'path': path}

    def disconnect_volume(self, connection_properties, device_info):
        """No need to do anything to disconnect a volume in a filesystem."""


class RBDConnector(InitiatorConnector):
    """"Connector class to attach/detach RBD volumes."""

    def __init__(self, root_helper, driver=None,
                 execute=putils.execute, use_multipath=False,
                 device_scan_attempts=DEVICE_SCAN_ATTEMPTS_DEFAULT,
                 *args, **kwargs):

        super(RBDConnector, self).__init__(root_helper, driver=driver,
                                           execute=execute,
                                           device_scan_attempts=
                                           device_scan_attempts,
                                           *args, **kwargs)

    def connect_volume(self, connection_properties):
        """Connect to a volume."""
        try:
            user = connection_properties['auth_username']
            pool, volume = connection_properties['name'].split('/')
        except IndexError:
            msg = _("Connect volume failed, malformed connection properties")
            raise exception.BrickException(msg=msg)

        rbd_client = linuxrbd.RBDClient(user, pool)
        rbd_volume = linuxrbd.RBDVolume(rbd_client, volume)
        rbd_handle = linuxrbd.RBDVolumeIOWrapper(rbd_volume)

        return {'path': rbd_handle}

    def disconnect_volume(self, connection_properties, device_info):
        """Disconnect a volume."""
        rbd_handle = device_info.get('path', None)
        if rbd_handle is not None:
            rbd_handle.close()

    def check_valid_device(self, path, run_as_root=True):
        """Verify an existing RBD handle is connected and valid."""
        rbd_handle = path

        if rbd_handle is None:
            return False

        original_offset = rbd_handle.tell()

        try:
            rbd_handle.read(4096)
        except Exception as e:
            LOG.error(_LE("Failed to access RBD device handle: %(error)s"),
                      {"error": e})
            return False
        finally:
            rbd_handle.seek(original_offset, 0)

        return True


class LocalConnector(InitiatorConnector):
    """"Connector class to attach/detach File System backed volumes."""

    def __init__(self, root_helper, driver=None, execute=putils.execute,
                 *args, **kwargs):
        super(LocalConnector, self).__init__(root_helper, driver=driver,
                                             execute=execute, *args, **kwargs)

    def connect_volume(self, connection_properties):
        """Connect to a volume.

        connection_properties must include:
        device_path - path to the volume to be connected
        """
        if 'device_path' not in connection_properties:
            msg = (_("Invalid connection_properties specified "
                     "no device_path attribute"))
            raise ValueError(msg)

        device_info = {'type': 'local',
                       'path': connection_properties['device_path']}
        return device_info

    def disconnect_volume(self, connection_properties, device_info):
        """Disconnect a volume from the local host."""
        pass


class HuaweiStorHyperConnector(InitiatorConnector):
    """"Connector class to attach/detach SDSHypervisor volumes."""
    attached_success_code = 0
    has_been_attached_code = 50151401
    attach_mnid_done_code = 50151405
    vbs_unnormal_code = 50151209
    not_mount_node_code = 50155007
    iscliexist = True

    def __init__(self, root_helper, driver=None, execute=putils.execute,
                 *args, **kwargs):
        self.cli_path = os.getenv('HUAWEISDSHYPERVISORCLI_PATH')
        if not self.cli_path:
            self.cli_path = '/usr/local/bin/sds/sds_cli'
            LOG.debug("CLI path is not configured, using default %s.",
                      self.cli_path)
        if not os.path.isfile(self.cli_path):
            self.iscliexist = False
            LOG.error(_LE('SDS CLI file not found, '
                          'HuaweiStorHyperConnector init failed.'))
        super(HuaweiStorHyperConnector, self).__init__(root_helper,
                                                       driver=driver,
                                                       execute=execute,
                                                       *args, **kwargs)

    @synchronized('connect_volume')
    def connect_volume(self, connection_properties):
        """Connect to a volume."""
        LOG.debug("Connect_volume connection properties: %s.",
                  connection_properties)
        out = self._attach_volume(connection_properties['volume_id'])
        if not out or int(out['ret_code']) not in (self.attached_success_code,
                                                   self.has_been_attached_code,
                                                   self.attach_mnid_done_code):
            msg = (_("Attach volume failed, "
                   "error code is %s") % out['ret_code'])
            raise exception.BrickException(message=msg)
        out = self._query_attached_volume(
            connection_properties['volume_id'])
        if not out or int(out['ret_code']) != 0:
            msg = _("query attached volume failed or volume not attached.")
            raise exception.BrickException(message=msg)

        device_info = {'type': 'block',
                       'path': out['dev_addr']}
        return device_info

    @synchronized('connect_volume')
    def disconnect_volume(self, connection_properties, device_info):
        """Disconnect a volume from the local host."""
        LOG.debug("Disconnect_volume: %s.", connection_properties)
        out = self._detach_volume(connection_properties['volume_id'])
        if not out or int(out['ret_code']) not in (self.attached_success_code,
                                                   self.vbs_unnormal_code,
                                                   self.not_mount_node_code):
            msg = (_("Disconnect_volume failed, "
                   "error code is %s") % out['ret_code'])
            raise exception.BrickException(message=msg)

    def is_volume_connected(self, volume_name):
        """Check if volume already connected to host"""
        LOG.debug('Check if volume %s already connected to a host.',
                  volume_name)
        out = self._query_attached_volume(volume_name)
        if out:
            return int(out['ret_code']) == 0
        return False

    def _attach_volume(self, volume_name):
        return self._cli_cmd('attach', volume_name)

    def _detach_volume(self, volume_name):
        return self._cli_cmd('detach', volume_name)

    def _query_attached_volume(self, volume_name):
        return self._cli_cmd('querydev', volume_name)

    def _cli_cmd(self, method, volume_name):
        LOG.debug("Enter into _cli_cmd.")
        if not self.iscliexist:
            msg = _("SDS command line doesn't exist, "
                    "can't execute SDS command.")
            raise exception.BrickException(message=msg)
        if not method or volume_name is None:
            return
        cmd = [self.cli_path, '-c', method, '-v', volume_name]
        out, clilog = self._execute(*cmd, run_as_root=False,
                                    root_helper=self._root_helper)
        analyse_result = self._analyze_output(out)
        LOG.debug('%(method)s volume returns %(analyse_result)s.',
                  {'method': method, 'analyse_result': analyse_result})
        if clilog:
            LOG.error(_LE("SDS CLI output some log: %s."), clilog)
        return analyse_result

    def _analyze_output(self, out):
        LOG.debug("Enter into _analyze_output.")
        if out:
            analyse_result = {}
            out_temp = out.split('\n')
            for line in out_temp:
                LOG.debug("Line is %s.", line)
                if line.find('=') != -1:
                    key, val = line.split('=', 1)
                    LOG.debug("%(key)s = %(val)s", {'key': key, 'val': val})
                    if key in ['ret_code', 'ret_desc', 'dev_addr']:
                        analyse_result[key] = val
            return analyse_result
        else:
            return None


class HGSTConnector(InitiatorConnector):
    """Connector class to attach/detach HGST volumes."""
    VGCCLUSTER = 'vgc-cluster'

    def __init__(self, root_helper, driver=None,
                 execute=putils.execute,
                 device_scan_attempts=DEVICE_SCAN_ATTEMPTS_DEFAULT,
                 *args, **kwargs):
        super(HGSTConnector, self).__init__(root_helper, driver=driver,
                                            execute=execute,
                                            device_scan_attempts=
                                            device_scan_attempts,
                                            *args, **kwargs)
        self._vgc_host = None

    def _log_cli_err(self, err):
        """Dumps the full command output to a logfile in error cases."""
        LOG.error(_LE("CLI fail: '%(cmd)s' = %(code)s\nout: %(stdout)s\n"
                      "err: %(stderr)s"),
                  {'cmd': err.cmd, 'code': err.exit_code,
                   'stdout': err.stdout, 'stderr': err.stderr})

    def _find_vgc_host(self):
        """Finds vgc-cluster hostname for this box."""
        params = [self.VGCCLUSTER, "domain-list", "-1"]
        try:
            out, unused = self._execute(*params, run_as_root=True,
                                        root_helper=self._root_helper)
        except putils.ProcessExecutionError as err:
            self._log_cli_err(err)
            msg = _("Unable to get list of domain members, check that "
                    "the cluster is running.")
            raise exception.BrickException(message=msg)
        domain = out.splitlines()
        params = ["ip", "addr", "list"]
        try:
            out, unused = self._execute(*params, run_as_root=False)
        except putils.ProcessExecutionError as err:
            self._log_cli_err(err)
            msg = _("Unable to get list of IP addresses on this host, "
                    "check permissions and networking.")
            raise exception.BrickException(message=msg)
        nets = out.splitlines()
        for host in domain:
            try:
                ip = socket.gethostbyname(host)
                for l in nets:
                    x = l.strip()
                    if x.startswith("inet %s/" % ip):
                        return host
            except socket.error:
                pass
        msg = _("Current host isn't part of HGST domain.")
        raise exception.BrickException(message=msg)

    def _hostname(self):
        """Returns hostname to use for cluster operations on this box."""
        if self._vgc_host is None:
            self._vgc_host = self._find_vgc_host()
        return self._vgc_host

    def connect_volume(self, connection_properties):
        """Attach a Space volume to running host.

        connection_properties for HGST must include:
        name - Name of space to attach
        """
        if connection_properties is None:
            msg = _("Connection properties passed in as None.")
            raise exception.BrickException(message=msg)
        if 'name' not in connection_properties:
            msg = _("Connection properties missing 'name' field.")
            raise exception.BrickException(message=msg)
        device_info = {
            'type': 'block',
            'device': connection_properties['name'],
            'path': '/dev/' + connection_properties['name']
        }
        volname = device_info['device']
        params = [self.VGCCLUSTER, 'space-set-apphosts']
        params += ['-n', volname]
        params += ['-A', self._hostname()]
        params += ['--action', 'ADD']
        try:
            self._execute(*params, run_as_root=True,
                          root_helper=self._root_helper)
        except putils.ProcessExecutionError as err:
            self._log_cli_err(err)
            msg = (_("Unable to set apphost for space %s") % volname)
            raise exception.BrickException(message=msg)

        return device_info

    def disconnect_volume(self, connection_properties, device_info):
        """Detach and flush the volume.

        connection_properties for HGST must include:
        name - Name of space to detach
        noremovehost - Host which should never be removed
        """
        if connection_properties is None:
            msg = _("Connection properties passed in as None.")
            raise exception.BrickException(message=msg)
        if 'name' not in connection_properties:
            msg = _("Connection properties missing 'name' field.")
            raise exception.BrickException(message=msg)
        if 'noremovehost' not in connection_properties:
            msg = _("Connection properties missing 'noremovehost' field.")
            raise exception.BrickException(message=msg)
        if connection_properties['noremovehost'] != self._hostname():
            params = [self.VGCCLUSTER, 'space-set-apphosts']
            params += ['-n', connection_properties['name']]
            params += ['-A', self._hostname()]
            params += ['--action', 'DELETE']
            try:
                self._execute(*params, run_as_root=True,
                              root_helper=self._root_helper)
            except putils.ProcessExecutionError as err:
                self._log_cli_err(err)
                msg = (_("Unable to set apphost for space %s") %
                       connection_properties['name'])
                raise exception.BrickException(message=msg)


class ScaleIOConnector(InitiatorConnector):
    """Class implements the connector driver for ScaleIO."""
    OK_STATUS_CODE = 200
    VOLUME_NOT_MAPPED_ERROR = 84
    VOLUME_ALREADY_MAPPED_ERROR = 81
    GET_GUID_CMD = ['drv_cfg', '--query_guid']

    def __init__(self, root_helper, driver=None, execute=putils.execute,
                 device_scan_attempts=DEVICE_SCAN_ATTEMPTS_DEFAULT,
                 *args, **kwargs):
        super(ScaleIOConnector, self).__init__(
            root_helper,
            driver=driver,
            execute=execute,
            device_scan_attempts=device_scan_attempts,
            *args, **kwargs
        )

        self.local_sdc_ip = None
        self.server_ip = None
        self.server_port = None
        self.server_username = None
        self.server_password = None
        self.server_token = None
        self.volume_id = None
        self.volume_name = None
        self.volume_path = None
        self.iops_limit = None
        self.bandwidth_limit = None

    def _find_volume_path(self):
        LOG.info(_LI(
            "Looking for volume %(volume_id)s, maximum tries: %(tries)s"),
            {'volume_id': self.volume_id, 'tries': self.device_scan_attempts}
        )

        # look for the volume in /dev/disk/by-id directory
        by_id_path = "/dev/disk/by-id"

        disk_filename = self._wait_for_volume_path(by_id_path)
        full_disk_name = ("%(path)s/%(filename)s" %
                          {'path': by_id_path, 'filename': disk_filename})
        LOG.info(_LI("Full disk name is %(full_path)s"),
                 {'full_path': full_disk_name})
        return full_disk_name

    # NOTE: Usually 3 retries is enough to find the volume.
    # If there are network issues, it could take much longer. Set
    # the max retries to 15 to make sure we can find the volume.
    @utils.retry(exceptions=exception.BrickException,
                 retries=15,
                 backoff_rate=1)
    def _wait_for_volume_path(self, path):
        if not os.path.isdir(path):
            msg = (
                _("ScaleIO volume %(volume_id)s not found at "
                  "expected path.") % {'volume_id': self.volume_id}
                )

            LOG.debug(msg)
            raise exception.BrickException(message=msg)

        disk_filename = None
        filenames = os.listdir(path)
        LOG.info(_LI(
            "Files found in %(path)s path: %(files)s "),
            {'path': path, 'files': filenames}
        )

        for filename in filenames:
            if (filename.startswith("emc-vol") and
                    filename.endswith(self.volume_id)):
                disk_filename = filename
                break

        if not disk_filename:
            msg = (_("ScaleIO volume %(volume_id)s not found.") %
                   {'volume_id': self.volume_id})
            LOG.debug(msg)
            raise exception.BrickException(message=msg)

        return disk_filename

    def _get_client_id(self):
        request = (
            "https://%(server_ip)s:%(server_port)s/"
            "api/types/Client/instances/getByIp::%(sdc_ip)s/" %
            {
                'server_ip': self.server_ip,
                'server_port': self.server_port,
                'sdc_ip': self.local_sdc_ip
            }
        )

        LOG.info(_LI("ScaleIO get client id by ip request: %(request)s"),
                 {'request': request})

        r = requests.get(
            request,
            auth=(self.server_username, self.server_token),
            verify=False
        )

        r = self._check_response(r, request)
        sdc_id = r.json()
        if not sdc_id:
            msg = (_("Client with ip %(sdc_ip)s was not found.") %
                   {'sdc_ip': self.local_sdc_ip})
            raise exception.BrickException(message=msg)

        if r.status_code != 200 and "errorCode" in sdc_id:
            msg = (_("Error getting sdc id from ip %(sdc_ip)s: %(err)s") %
                   {'sdc_ip': self.local_sdc_ip, 'err': sdc_id['message']})

            LOG.error(msg)
            raise exception.BrickException(message=msg)

        LOG.info(_LI("ScaleIO sdc id is %(sdc_id)s."),
                 {'sdc_id': sdc_id})
        return sdc_id

    def _get_volume_id(self):
        volname_encoded = urllib.parse.quote(self.volume_name, '')
        volname_double_encoded = urllib.parse.quote(volname_encoded, '')
        LOG.debug(_(
            "Volume name after double encoding is %(volume_name)s."),
            {'volume_name': volname_double_encoded}
        )

        request = (
            "https://%(server_ip)s:%(server_port)s/api/types/Volume/instances"
            "/getByName::%(encoded_volume_name)s" %
            {
                'server_ip': self.server_ip,
                'server_port': self.server_port,
                'encoded_volume_name': volname_double_encoded
            }
        )

        LOG.info(
            _LI("ScaleIO get volume id by name request: %(request)s"),
            {'request': request}
        )

        r = requests.get(request,
                         auth=(self.server_username, self.server_token),
                         verify=False)

        r = self._check_response(r, request)

        volume_id = r.json()
        if not volume_id:
            msg = (_("Volume with name %(volume_name)s wasn't found.") %
                   {'volume_name': self.volume_name})

            LOG.error(msg)
            raise exception.BrickException(message=msg)

        if r.status_code != self.OK_STATUS_CODE and "errorCode" in volume_id:
            msg = (
                _("Error getting volume id from name %(volume_name)s: "
                  "%(err)s") %
                {'volume_name': self.volume_name, 'err': volume_id['message']}
            )

            LOG.error(msg)
            raise exception.BrickException(message=msg)

        LOG.info(_LI("ScaleIO volume id is %(volume_id)s."),
                 {'volume_id': volume_id})
        return volume_id

    def _check_response(self, response, request, is_get_request=True,
                        params=None):
        if response.status_code == 401 or response.status_code == 403:
            LOG.info(_LI("Token is invalid, "
                         "going to re-login to get a new one"))

            login_request = (
                "https://%(server_ip)s:%(server_port)s/api/login" %
                {'server_ip': self.server_ip, 'server_port': self.server_port}
            )

            r = requests.get(
                login_request,
                auth=(self.server_username, self.server_password),
                verify=False
            )

            token = r.json()
            # repeat request with valid token
            LOG.debug(_("Going to perform request %(request)s again "
                        "with valid token"), {'request': request})

            if is_get_request:
                res = requests.get(request,
                                   auth=(self.server_username, token),
                                   verify=False)
            else:
                headers = {'content-type': 'application/json'}
                res = requests.post(
                    request,
                    data=json.dumps(params),
                    headers=headers,
                    auth=(self.server_username, token),
                    verify=False
                )

            self.server_token = token
            return res

        return response

    def get_config(self, connection_properties):
        self.local_sdc_ip = connection_properties['hostIP']
        self.volume_name = connection_properties['scaleIO_volname']
        self.server_ip = connection_properties['serverIP']
        self.server_port = connection_properties['serverPort']
        self.server_username = connection_properties['serverUsername']
        self.server_password = connection_properties['serverPassword']
        self.server_token = connection_properties['serverToken']
        self.iops_limit = connection_properties['iopsLimit']
        self.bandwidth_limit = connection_properties['bandwidthLimit']
        device_info = {'type': 'block',
                       'path': self.volume_path}
        return device_info

    @lockutils.synchronized('scaleio', 'scaleio-')
    def connect_volume(self, connection_properties):
        """Connect the volume."""
        device_info = self.get_config(connection_properties)
        LOG.debug(
            _(
                "scaleIO Volume name: %(volume_name)s, SDC IP: %(sdc_ip)s, "
                "REST Server IP: %(server_ip)s, "
                "REST Server username: %(username)s, "
                "iops limit:%(iops_limit)s, "
                "bandwidth limit: %(bandwidth_limit)s."
            ), {
                'volume_name': self.volume_name,
                'sdc_ip': self.local_sdc_ip,
                'server_ip': self.server_ip,
                'username': self.server_username,
                'iops_limit': self.iops_limit,
                'bandwidth_limit': self.bandwidth_limit
            }
        )

        LOG.info(_LI("ScaleIO sdc query guid command: %(cmd)s"),
                 {'cmd': self.GET_GUID_CMD})

        try:
            (out, err) = self._execute(*self.GET_GUID_CMD, run_as_root=True,
                                       root_helper=self._root_helper)

            LOG.info(_LI("Map volume %(cmd)s: stdout=%(out)s "
                         "stderr=%(err)s"),
                     {'cmd': self.GET_GUID_CMD, 'out': out, 'err': err})

        except putils.ProcessExecutionError as e:
            msg = (_("Error querying sdc guid: %(err)s") % {'err': e.stderr})
            LOG.error(msg)
            raise exception.BrickException(message=msg)

        guid = out
        LOG.info(_LI("Current sdc guid: %(guid)s"), {'guid': guid})
        params = {'guid': guid, 'allowMultipleMappings': 'TRUE'}
        self.volume_id = self._get_volume_id()

        headers = {'content-type': 'application/json'}
        request = (
            "https://%(server_ip)s:%(server_port)s/api/instances/"
            "Volume::%(volume_id)s/action/addMappedSdc" %
            {'server_ip': self.server_ip, 'server_port': self.server_port,
             'volume_id': self.volume_id}
        )

        LOG.info(_LI("map volume request: %(request)s"), {'request': request})
        r = requests.post(
            request,
            data=json.dumps(params),
            headers=headers,
            auth=(self.server_username, self.server_token),
            verify=False
        )

        r = self._check_response(r, request, False, params)
        if r.status_code != self.OK_STATUS_CODE:
            response = r.json()
            error_code = response['errorCode']
            if error_code == self.VOLUME_ALREADY_MAPPED_ERROR:
                LOG.warning(_LW(
                    "Ignoring error mapping volume %(volume_name)s: "
                    "volume already mapped."),
                    {'volume_name': self.volume_name}
                )
            else:
                msg = (
                    _("Error mapping volume %(volume_name)s: %(err)s") %
                    {'volume_name': self.volume_name,
                     'err': response['message']}
                )

                LOG.error(msg)
                raise exception.BrickException(message=msg)

        self.volume_path = self._find_volume_path()
        device_info['path'] = self.volume_path

        # Set QoS settings after map was performed
        if self.iops_limit is not None or self.bandwidth_limit is not None:
            params = {'guid': guid}
            if self.bandwidth_limit is not None:
                params['bandwidthLimitInKbps'] = self.bandwidth_limit
            if self.iops_limit is not None:
                params['iopsLimit'] = self.iops_limit

            request = (
                "https://%(server_ip)s:%(server_port)s/api/instances/"
                "Volume::%(volume_id)s/action/setMappedSdcLimits" %
                {'server_ip': self.server_ip, 'server_port': self.server_port,
                 'volume_id': self.volume_id}
            )

            LOG.info(_LI("Set client limit request: %(request)s"),
                     {'request': request})

            r = requests.post(
                request,
                data=json.dumps(params),
                headers=headers,
                auth=(self.server_username, self.server_token),
                verify=False
            )
            r = self._check_response(r, request, False, params)
            if r.status_code != self.OK_STATUS_CODE:
                response = r.json()
                LOG.info(_LI("Set client limit response: %(response)s"),
                         {'response': response})
                msg = (
                    _("Error setting client limits for volume "
                      "%(volume_name)s: %(err)s") %
                    {'volume_name': self.volume_name,
                     'err': response['message']}
                )

                LOG.error(msg)

        return device_info

    @lockutils.synchronized('scaleio', 'scaleio-')
    def disconnect_volume(self, connection_properties, device_info):
        self.get_config(connection_properties)
        self.volume_id = self._get_volume_id()
        LOG.info(_LI(
            "ScaleIO disconnect volume in ScaleIO brick volume driver."
        ))

        LOG.debug(
            _("ScaleIO Volume name: %(volume_name)s, SDC IP: %(sdc_ip)s, "
              "REST Server IP: %(server_ip)s"),
            {'volume_name': self.volume_name, 'sdc_ip': self.local_sdc_ip,
             'server_ip': self.server_ip}
        )

        LOG.info(_LI("ScaleIO sdc query guid command: %(cmd)s"),
                 {'cmd': self.GET_GUID_CMD})

        try:
            (out, err) = self._execute(*self.GET_GUID_CMD, run_as_root=True,
                                       root_helper=self._root_helper)
            LOG.info(
                _LI("Unmap volume %(cmd)s: stdout=%(out)s stderr=%(err)s"),
                {'cmd': self.GET_GUID_CMD, 'out': out, 'err': err}
            )

        except putils.ProcessExecutionError as e:
            msg = _("Error querying sdc guid: %(err)s") % {'err': e.stderr}
            LOG.error(msg)
            raise exception.BrickException(message=msg)

        guid = out
        LOG.info(_LI("Current sdc guid: %(guid)s"), {'guid': guid})

        params = {'guid': guid}
        headers = {'content-type': 'application/json'}
        request = (
            "https://%(server_ip)s:%(server_port)s/api/instances/"
            "Volume::%(volume_id)s/action/removeMappedSdc" %
            {'server_ip': self.server_ip, 'server_port': self.server_port,
             'volume_id': self.volume_id}
        )

        LOG.info(_LI("Unmap volume request: %(request)s"),
                 {'request': request})
        r = requests.post(
            request,
            data=json.dumps(params),
            headers=headers,
            auth=(self.server_username, self.server_token),
            verify=False
        )

        r = self._check_response(r, request, False, params)
        if r.status_code != self.OK_STATUS_CODE:
            response = r.json()
            error_code = response['errorCode']
            if error_code == self.VOLUME_NOT_MAPPED_ERROR:
                LOG.warning(_LW(
                    "Ignoring error unmapping volume %(volume_id)s: "
                    "volume not mapped."), {'volume_id': self.volume_name}
                )
            else:
                msg = (_("Error unmapping volume %(volume_id)s: %(err)s") %
                       {'volume_id': self.volume_name,
                        'err': response['message']})
                LOG.error(msg)
                raise exception.BrickException(message=msg)


class StorPoolConnector(InitiatorConnector):
    def __init__(self, root_helper, driver=None, execute=putils.execute,
                 *args, **kwargs):
        self._execute = execute
        self._root_helper = root_helper
        try:
            from storpool import spopenstack
        except ImportError:
            raise exception.BrickException(_LE(
                'Could not import the StorPool API bindings.'))
        try:
            self._attach = spopenstack.AttachDB(log=LOG)
            self._attach.api()
        except Exception as e:
            raise exception.BrickException(_LE(
                'Could not initialize the StorPool API bindings: %s') % (e))

    def connect_volume(self, connection_properties):
        client_id = connection_properties.get('client_id', None)
        if client_id is None:
            raise exception.BrickException(_LE(
                'Invalid StorPool connection data, no client ID specified.'))
        volume_id = connection_properties.get('volume', None)
        if volume_id is None:
            raise exception.BrickException(_LE(
                'Invalid StorPool connection data, no volume ID specified.'))
        volume = self._attach.volumeName(volume_id)
        mode = connection_properties.get('access_mode', None)
        if mode is None or mode not in ('rw', 'ro'):
            raise exception.BrickException(_LE(
                'Invalid access_mode specified in the connection data.'))
        req_id = 'brick-%s-%s' % (client_id, volume_id)
        self._attach.add(req_id, {
            'volume': volume,
            'type': 'brick',
            'id': req_id,
            'rights': 1 if mode == 'ro' else 2,
            'volsnap': False
        })
        self._attach.sync(req_id, None)
        return {'type': 'block', 'path': '/dev/storpool/' + volume}

    def disconnect_volume(self, connection_properties, device_info):
        client_id = connection_properties.get('client_id', None)
        if client_id is None:
            raise exception.BrickException(_LE(
                'Invalid StorPool connection data, no client ID specified.'))
        volume_id = connection_properties.get('volume', None)
        if volume_id is None:
            raise exception.BrickException(_LE(
                'Invalid StorPool connection data, no volume ID specified.'))
        volume = self._attach.volumeName(volume_id)
        req_id = 'brick-%s-%s' % (client_id, volume_id)
        self._attach.sync(req_id, volume)
        self._attach.remove(req_id)
