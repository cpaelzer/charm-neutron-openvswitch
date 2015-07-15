import os
import shutil

from charmhelpers.contrib.openstack.neutron import neutron_plugin_attribute
from copy import deepcopy

from charmhelpers.contrib.openstack import context, templating
from charmhelpers.contrib.openstack.utils import (
    git_install_requested,
    git_clone_and_install,
    git_src_dir,
    git_pip_venv_dir,
)
from collections import OrderedDict
from charmhelpers.contrib.openstack.utils import (
    os_release,
)
import neutron_ovs_context
from charmhelpers.contrib.network.ovs import (
    add_bridge,
    add_bridge_port,
    full_restart,
)
from charmhelpers.core.hookenv import (
    config,
)
from charmhelpers.contrib.openstack.neutron import (
    parse_bridge_mappings,
)
from charmhelpers.contrib.openstack.context import (
    ExternalPortContext,
    DataPortContext,
)
from charmhelpers.core.host import (
    adduser,
    add_group,
    add_user_to_group,
    mkdir,
    service_restart,
    service_running,
    write_file,
)

from charmhelpers.core.templating import render

BASE_GIT_PACKAGES = [
    'libffi-dev',
    'libssl-dev',
    'libxml2-dev',
    'libxslt1-dev',
    'libyaml-dev',
    'openvswitch-switch',
    'python-dev',
    'python-pip',
    'python-setuptools',
    'zlib1g-dev',
]

# ubuntu packages that should not be installed when deploying from git
GIT_PACKAGE_BLACKLIST = [
    'neutron-l3-agent',
    'neutron-metadata-agent',
    'neutron-server',
    'neutron-plugin-openvswitch',
    'neutron-plugin-openvswitch-agent',
]

NOVA_CONF_DIR = "/etc/nova"
NEUTRON_CONF_DIR = "/etc/neutron"
NEUTRON_CONF = '%s/neutron.conf' % NEUTRON_CONF_DIR
NEUTRON_DEFAULT = '/etc/default/neutron-server'
NEUTRON_L3_AGENT_CONF = "/etc/neutron/l3_agent.ini"
NEUTRON_FWAAS_CONF = "/etc/neutron/fwaas_driver.ini"
ML2_CONF = '%s/plugins/ml2/ml2_conf.ini' % NEUTRON_CONF_DIR
EXT_PORT_CONF = '/etc/init/ext-port.conf'
NEUTRON_METADATA_AGENT_CONF = "/etc/neutron/metadata_agent.ini"
DVR_PACKAGES = ['neutron-l3-agent']
PHY_NIC_MTU_CONF = '/etc/init/os-charm-phy-nic-mtu.conf'
TEMPLATES = 'templates/'

BASE_RESOURCE_MAP = OrderedDict([
    (NEUTRON_CONF, {
        'services': ['neutron-plugin-openvswitch-agent'],
        'contexts': [neutron_ovs_context.OVSPluginContext(),
                     context.AMQPContext(ssl_dir=NEUTRON_CONF_DIR),
                     context.ZeroMQContext(),
                     context.NotificationDriverContext()],
    }),
    (ML2_CONF, {
        'services': ['neutron-plugin-openvswitch-agent'],
        'contexts': [neutron_ovs_context.OVSPluginContext()],
    }),
    (PHY_NIC_MTU_CONF, {
        'services': ['os-charm-phy-nic-mtu'],
        'contexts': [context.PhyNICMTUContext()],
    }),
])
DVR_RESOURCE_MAP = OrderedDict([
    (NEUTRON_L3_AGENT_CONF, {
        'services': ['neutron-l3-agent'],
        'contexts': [neutron_ovs_context.L3AgentContext()],
    }),
    (NEUTRON_FWAAS_CONF, {
        'services': ['neutron-l3-agent'],
        'contexts': [neutron_ovs_context.L3AgentContext()],
    }),
    (EXT_PORT_CONF, {
        'services': ['neutron-l3-agent'],
        'contexts': [context.ExternalPortContext()],
    }),
    (NEUTRON_METADATA_AGENT_CONF, {
        'services': ['neutron-metadata-agent'],
        'contexts': [neutron_ovs_context.DVRSharedSecretContext(),
                     neutron_ovs_context.APIIdentityServiceContext()],
    }),
])
TEMPLATES = 'templates/'
INT_BRIDGE = "br-int"
EXT_BRIDGE = "br-ex"
DATA_BRIDGE = 'br-data'


def determine_dvr_packages():
    if not git_install_requested():
        if use_dvr():
            return DVR_PACKAGES
    return []


def determine_packages():
    pkgs = neutron_plugin_attribute('ovs', 'packages', 'neutron')
    pkgs.extend(determine_dvr_packages())

    if git_install_requested():
        pkgs.extend(BASE_GIT_PACKAGES)
        # don't include packages that will be installed from git
        for p in GIT_PACKAGE_BLACKLIST:
            if p in pkgs:
                pkgs.remove(p)

    return pkgs


def register_configs(release=None):
    release = release or os_release('neutron-common', base='icehouse')
    configs = templating.OSConfigRenderer(templates_dir=TEMPLATES,
                                          openstack_release=release)
    for cfg, rscs in resource_map().iteritems():
        configs.register(cfg, rscs['contexts'])
    return configs


def resource_map():
    '''
    Dynamically generate a map of resources that will be managed for a single
    hook execution.
    '''
    resource_map = deepcopy(BASE_RESOURCE_MAP)
    if use_dvr():
        resource_map.update(DVR_RESOURCE_MAP)
        dvr_services = ['neutron-metadata-agent', 'neutron-l3-agent']
        resource_map[NEUTRON_CONF]['services'] += dvr_services
    return resource_map


def restart_map():
    '''
    Constructs a restart map based on charm config settings and relation
    state.
    '''
    return {k: v['services'] for k, v in resource_map().iteritems()}


def get_topics():
    topics = []
    topics.append('q-agent-notifier-port-update')
    topics.append('q-agent-notifier-network-delete')
    topics.append('q-agent-notifier-tunnel-update')
    topics.append('q-agent-notifier-security_group-update')
    topics.append('q-agent-notifier-dvr-update')
    if context.NeutronAPIContext()()['l2_population']:
        topics.append('q-agent-notifier-l2population-update')
    return topics


def configure_ovs():
    if not service_running('openvswitch-switch'):
        full_restart()
    add_bridge(INT_BRIDGE)
    add_bridge(EXT_BRIDGE)
    ext_port_ctx = None
    if use_dvr():
        ext_port_ctx = ExternalPortContext()()
    if ext_port_ctx and ext_port_ctx['ext_port']:
        add_bridge_port(EXT_BRIDGE, ext_port_ctx['ext_port'])

    portmaps = DataPortContext()()
    bridgemaps = parse_bridge_mappings(config('bridge-mappings'))
    for provider, br in bridgemaps.iteritems():
        add_bridge(br)
        if not portmaps or br not in portmaps:
            continue

        add_bridge_port(br, portmaps[br], promisc=True)

    # Ensure this runs so that mtu is applied to data-port interfaces if
    # provided.
    service_restart('os-charm-phy-nic-mtu')


def get_shared_secret():
    ctxt = neutron_ovs_context.DVRSharedSecretContext()()
    if 'shared_secret' in ctxt:
        return ctxt['shared_secret']


def use_dvr():
    return context.NeutronAPIContext()()['enable_dvr']


def git_install(projects_yaml):
    """Perform setup, and install git repos specified in yaml parameter."""
    if git_install_requested():
        git_pre_install()
        git_clone_and_install(projects_yaml, core_project='neutron')
        git_post_install(projects_yaml)


def git_pre_install():
    """Perform pre-install setup."""
    dirs = [
        '/var/lib/neutron',
        '/var/lib/neutron/lock',
        '/var/log/neutron',
    ]

    logs = [
        '/var/log/neutron/server.log',
    ]

    adduser('neutron', shell='/bin/bash', system_user=True)
    add_group('neutron', system_group=True)
    add_user_to_group('neutron', 'neutron')

    for d in dirs:
        mkdir(d, owner='neutron', group='neutron', perms=0755, force=False)

    for l in logs:
        write_file(l, '', owner='neutron', group='neutron', perms=0600)


def git_post_install(projects_yaml):
    """Perform post-install setup."""
    src_etc = os.path.join(git_src_dir(projects_yaml, 'neutron'), 'etc')
    configs = [
        {'src': src_etc,
         'dest': '/etc/neutron'},
        {'src': os.path.join(src_etc, 'neutron/plugins'),
         'dest': '/etc/neutron/plugins'},
        {'src': os.path.join(src_etc, 'neutron/rootwrap.d'),
         'dest': '/etc/neutron/rootwrap.d'},
    ]

    for c in configs:
        if os.path.exists(c['dest']):
            shutil.rmtree(c['dest'])
        shutil.copytree(c['src'], c['dest'])

    # NOTE(coreycb): Need to find better solution than bin symlinks.
    symlinks = [
        {'src': os.path.join(git_pip_venv_dir(projects_yaml),
                             'bin/neutron-rootwrap'),
         'link': '/usr/local/bin/neutron-rootwrap'},
    ]

    for s in symlinks:
        if os.path.lexists(s['link']):
            os.remove(s['link'])
        os.symlink(s['src'], s['link'])

    render('git/neutron_sudoers', '/etc/sudoers.d/neutron_sudoers', {},
           perms=0o440)

    bin_dir = os.path.join(git_pip_venv_dir(projects_yaml), 'bin')
    neutron_ovs_agent_context = {
        'service_description': 'Neutron OpenvSwitch Plugin Agent',
        'charm_name': 'neutron-openvswitch',
        'process_name': 'neutron-openvswitch-agent',
        'executable_name': os.path.join(bin_dir, 'neutron-openvswitch-agent'),
        'cleanup_process_name': 'neutron-ovs-cleanup',
        'plugin_config': '/etc/neutron/plugins/ml2/ml2_conf.ini',
        'log_file': '/var/log/neutron/openvswitch-agent.log',
    }

    neutron_ovs_cleanup_context = {
        'service_description': 'Neutron OpenvSwitch Cleanup',
        'charm_name': 'neutron-openvswitch',
        'process_name': 'neutron-ovs-cleanup',
        'executable_name': os.path.join(bin_dir, 'neutron-ovs-cleanup'),
        'log_file': '/var/log/neutron/ovs-cleanup.log',
    }

    # NOTE(coreycb): Needs systemd support
    render('git/upstart/neutron-plugin-openvswitch-agent.upstart',
           '/etc/init/neutron-plugin-openvswitch-agent.conf',
           neutron_ovs_agent_context, perms=0o644)
    render('git/upstart/neutron-ovs-cleanup.upstart',
           '/etc/init/neutron-ovs-cleanup.conf',
           neutron_ovs_cleanup_context, perms=0o644)

    service_restart('neutron-plugin-openvswitch-agent')
