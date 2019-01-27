""" Zero-Touch Provisioning Script
This script downloads and installs software, performs stack renumbering, applies
a configuration template with $-based placeholders for variable substitutions
and can execute commands upon script completion, such as smart licensing
registration. A simple web server can be used to serve the script and software
to the device and standard syslog server can be used for script monitoring.
Finally, a DHCP server configured for option 67 is required.

Adapt the SYSLOG, JSON and DATA constants to your needs.

Supported platforms, software versions and other details can be found at:
https://cs.co/ztp_provisioning

Author:  Tim Dorssers
Version: 1.0
"""

import os
import re
import cli
import sys
import json
import time
import base64
from string import Template
from xml.dom import minidom
try:
    from urlparse import urljoin
except ImportError:
    from urllib.parse import urljoin

##### CONSTANTS ################################################################

SYSLOG = '10.0.0.1'  # Syslog IP address string, empty string disables syslog

# JSON is a string with URL of the JSON encoded DATA object as specified below.
# Empty string disables downloading of external device data.
JSON = 'http://10.0.0.1:8080/data'

# DATA is a list of dicts that defines device data. To specify device defaults,
# omit the key named 'stack' from one dict. Empty list disables the internal
# data of the script. Valid keys and values are:
# 'stack'   : dict with target switch number as key and serial number as value
# 'version' : string with target version used to determine if upgrade is needed
# 'base_url': string with base URL to optionally join with install/config URL
# 'install' : string with URL of target IOS to download
# 'config'  : string with URL of configuration template to download
# 'subst'   : dict with keys that match the placeholders in the template
# 'cli'     : string of finishing commands separated by space and semicolon
# 'save'    : boolean to indicate to save configuration at script completion
# 'template': string holding configuration template with $-based placeholders
DATA = []

##### FUNCTIONS ################################################################

def log(severity, message):
    """ Sends string representation of message to stdout and IOS logging """
    print('\n%s' % str(message))
    sys.stdout.flush()  # force writing everything in the buffer to the terminal
    if SYSLOG:
        for line in str(message).splitlines():
            cli.execute('send log %d "%s"' % (severity, line))

def get_serials():
    """ Returns a dict with switch number as key and serial number as value """
    inventory = cli.execute('show inventory | format')  # xml formatted output
    doc = minidom.parseString(inventory)
    serials = {}
    for node in doc.getElementsByTagName('InventoryEntry'):
        chassis = node.getElementsByTagName('ChassisName')[0]
        # router
        if chassis.firstChild.data == '"Chassis"':
            serials[0] = node.getElementsByTagName('SN')[0].firstChild.data

        # switch
        match = re.match('"Switch ([0-9])"', chassis.firstChild.data)
        if match:
            unit = int(match.group(1))
            serials[unit] = node.getElementsByTagName('SN')[0].firstChild.data

    return serials

def is_iosxe_package(url):
    """ Returns True if the given file is an IOS-XE package """
    info = cli.execute('show file information %s' % url)
    # log error message if any and terminate script in case of failure
    match = re.match('^(%Error .*)', info)
    if match:
        log(3, match.group(1))
        shutdown(save=False, abnormal=True)

    match = re.search('type is (.*)', info)
    return 'IOSXE_PACKAGE' in match.group(1) if match else False

def get_version():
    """ Returns a string with the IOS version """
    version = cli.execute('show version')
    # extract version string
    match = re.search('Version ([A-Za-z0-9.:()]+)', version)
    # remove leading zeros from numbers
    ver_str = re.sub(r'\b0+(\d)', r'\1', match.group(1)) if match else 'unknown'
    # extract boot string
    match = re.search('System image file is "(.*)"', version)
    # check if the device started in bundle mode
    ver_str += ' bundle' if match and is_iosxe_package(match.group(1)) else ''
    return ver_str

def shutdown(save=False, abnormal=False):
    """ Cleansup and saves config if needed and terminates script """
    if save:
        log(6, 'Saving configuration upon script termination')

    if SYSLOG:
        cli.configure('''no logging host %s
            no logging discriminator ztp''' % SYSLOG)

    if save:
        cli.execute('copy running-config startup-config')

    sys.exit(int(abnormal))

def renumber_stack(stack, serials):
    """ Returns True if stack is renumbered or False otherwise """
    if stack is None:
        return False

    # renumber switches
    renumber = False
    for old_num in serials:
        for new_num in stack:
            if serials[old_num] == stack[new_num] and old_num != int(new_num):
                renumber = True
                # renumber switch and log error message in case of failure
                try:
                    cli.execute('switch {} renumber {}'.format(old_num, new_num))
                    log(6, 'Renumbered switch {} to {}'.format(old_num, new_num))
                except cli.CLISyntaxError as e:
                    log(3, e)
                    shutdown(save=False, abnormal=True)  # terminate script

    # set switch priorities
    switch = cli.execute('show switch')
    match = re.findall('(\d)\s+\S+\s+\S+\s+(\d+)', switch)
    for num, old_prio in match:
        new_prio = 16 - int(num)
        if (int(old_prio) != new_prio):
            # check if top switch is not active
            if switch.find('*{}'.format(sorted(serials.keys())[0])) == -1:
                renumber = True

            # set switch priority and log error message in case of failure
            try:
                cli.execute('switch %s priority %d' % (num, new_prio))
                log(6, 'Switch %s priority set to %d' % (num, new_prio))
            except cli.CLISyntaxError as e:
                log(3, e)
                shutdown(save=False, abnormal=True)  # terminate script

    if renumber:
        for num in serials.keys():
            # to prevent recovery from backup nvram
            try:
                cli.execute('delete flash-%s:nvram_config*' % num)
            except cli.CLISyntaxError as e:
                pass

    return renumber

def install(version, required, base_url, install_url, is_rtr):
    """ Returns True if install script is configured or False otherwise """
    # remove leading zeros from required version numbers and compare
    if (required is None or install_url is None
        or version == re.sub(r'\b0+(\d)', r'\1', required.strip())):
            return False

    install_url = urljoin(base_url, install_url)
    # terminate script in case of invalid file
    log(6, 'Checking %s' % install_url)
    if not is_iosxe_package(install_url):
        log(3, '%s is not valid image file' % install_url)
        shutdown(save=False, abnormal=True)

    # change boot mode if device is in bundle mode
    if 'bundle' in version:
        fs = 'bootflash:' if is_rtr else 'flash:'
        log(6, 'Changing the Boot Mode')
        cli.configure('''no boot system
            boot system {}packages.conf'''.format(fs))
        cli.execute('write memory')
        cli.execute('write erase')

    # Configure EEM applet for interactive command execution
    cli.configure('''event manager applet upgrade
        event none maxrun 900
        action 1.0 cli command "enable"
        action 2.0 syslog msg "Removing inactive images..."
        action 3.0 cli command "install remove inactive" pattern "\[y\/n\]|#"
        action 3.1 cli command "y"
        action 4.0 syslog msg "Downloading and installing image..."
        action 5.0 cli command "install add file %s activate commit" pattern "\[y\/n\/q\]|#"
        action 5.1 cli command "n" pattern "\[y\/n\]|#"
        action 5.2 cli command "y"
        action 6.0 syslog msg "Reloading stack..."
        action 7.0 reload''' % install_url)
    return True

def autoupgrade():
    """ Returns True if autoupgrade script is configured or False otherwise """
    switch = cli.execute('show switch')
    # look for a switch in version mismatch state
    if switch.find('V-Mismatch') > -1:
        # Workaround to execute interactive marked commands from guestshell
        cli.configure('''event manager applet upgrade
            event none maxrun 600
            action 1.0 cli command "enable"
            action 2.0 cli command "request platform software package install autoupgrade"
            action 3.0 syslog msg "Reloading stack..."
            action 4.0 reload''')
        return True
    else:
        return False

def parse_hex(fmt):
    """ Converts the hex/text format of the IOS more command to string """
    match = re.findall('\S{8}: +(\S{8} +\S{8} +\S{8} +\S{8})', fmt)
    parts = [base64.b16decode(re.sub(' |X', '', line)) for line in match]
    return ''.join(parts) if match else fmt

def download(file_url):
    """ Returns file contents or empty string in case of failure """
    if file_url:
        log(6, 'Downloading %s...' % file_url)
        result = cli.execute('more %s' % file_url)
        # log error message in case of failure
        match = re.match('^(%Error .*)', result)
        if match:
            log(3, match.group(1))

        # extract file contents from output
        match = re.search('^Loading %s (.*)' % file_url, result, re.DOTALL)
        return parse_hex(match.group(1)) if match else ''
    else:
        return ''

def apply_config(variables, base_url, config_url, template):
    """ Returns True if configuration template is applied successfully """
    if config_url:
        config_url = urljoin(base_url, config_url)

    # remove keyword 'end' from downloaded configuration
    conf = re.sub('^\s*end\s*$', '', download(config_url), flags=re.MULTILINE)
    if template:
        conf += '\n' + template if len(conf) else template

    if len(conf) == 0:
        return False

    # build configuration from template by $-based substitutions
    if variables:
        conf = Template(conf).safe_substitute(variables)

    # apply configuration and log error message in case of failure
    try:
        cli.configure(conf)
    except cli.CLIConfigurationError as e:
        log(3, 'Failed configurations:\n' + '\n'.join(map(str, e.failed)))
        shutdown(save=False, abnormal=True)  # terminate script
    else:
        return True

def blue_beacon(sw_nums):
    """ Turns on blue beacon of given switch number list, if supported """
    for num in sw_nums:
        # up to and including 16.8.x
        try:
            cli.cli('configure terminal ; hw-module beacon on switch %d' % num)
        except (cli.errors.cli_syntax_error, cli.errors.cli_exec_error):
            pass
        # from 16.9.x onwards
        try:
            cli.execute('hw-module beacon slot %d on' % num)
        except cli.CLISyntaxError:
            pass

def final_cli(command):
    """ Returns True if given command string is executed succesfully """
    if command is not None:
        try:
            cli.cli(command)
        except (cli.errors.cli_syntax_error, cli.errors.cli_exec_error) as e:
            log(3, e)
        else:
            return True
    else:
        return False

class Stack():
    """ Access to stack attributes with defaults, returns None if not found """
    def __init__(self, data, serials):
        """ Initializes object with data and serials """
        # absence of stack key indicates defaults dict
        self.defaults = next((dct for dct in data if not 'stack' in dct), {})
        # find dict with at least one common serial number in stack dict
        self.stack_dict = next((dct for dct in data if 'stack' in dct
                                and len(set(dct['stack'].values())
                                        & set(serials.values()))), {})

    def __getattr__(self, name):
        """ x.__getattr__(y) <==> x.y """
        return self.stack_dict.get(name, self.defaults.get(name, None))

def main():
    # setup IOS syslog for our own messages if server IP is specified
    if SYSLOG:
        cli.configure('''logging discriminator ztp msg-body includes Message from|HA_EM|INSTALL
            logging host %s discriminator ztp''' % SYSLOG)
        time.sleep(2)

    # show script name
    log(6, '*** Running %s ***' % os.path.basename(sys.argv[0]))
    # get platform serial numers and software version
    serials = get_serials()
    log(6, 'Platform serial number(s): %s' % ', '.join(serials.values()))
    version = get_version()
    log(6, 'Platform software version: %s' % version)
    # load JSON formatted data if URL is specified and concatenate it to DATA
    json_str = download(JSON)
    try:
        data = DATA + json.loads(json_str) if len(json_str) else DATA
    except ValueError as e:
        log(3, e)
        shutdown(save=False, abnormal=True)  # malformed data; terminate script

    # lookup stack in dataset, if not found turn on beacon
    my = Stack(data, serials)
    if my.stack is None:
        log(4, '% Stack not found in dataset')
        blue_beacon(serials.keys())
    else:
        # check if all specified switches are found, turn on beacon if not
        missing = set(my.stack.values()) - set(serials.values())
        if len(missing):
            log(4, 'Missing switch(es): %s' % ', '.join(missing))
            blue_beacon(serials.keys())

        # check if all found switches are specified, turn on beacon if not
        extra = set(serials.values()) - set(my.stack.values())
        if len(extra):
            log(4, 'Extra switch(es): %s' % ', '.join(extra))
            blue_beacon(serials.keys())

    is_rtr = 0 in serials
    # first, check version and install software if needed
    if install(version, my.version, my.base_url, my.install, is_rtr):
        log(6, 'Software upgrade starting asynchronously...')
        cli.execute('event manager run upgrade')
    else:
        # second, check v-mismatch and perform autoupgrade if needed
        if not is_rtr and autoupgrade():
            log(6, 'V-Mismatch detected, upgrade starting asynchronously...')
            cli.execute('event manager run upgrade')
        else:
            log(6, 'No software upgrade required')
            # third, check switch numbering and renumber stack if needed
            if not is_rtr and renumber_stack(my.stack, serials):
                log(6, 'Stack renumbered, reloading stack...')
                cli.execute('reload')
            else:
                log(6, 'No need to renumber stack')
                # fourth, apply configuration template if specified
                if apply_config(my.subst, my.base_url, my.config, my.template):
                    log(6, 'Configuration template applied successfully')
                # fifth, execute final cli if specified
                if final_cli(my.cli):
                    log(6, 'Final command(s) executed successfully')

                # cleanup after step 4 or 5 and save config if specified
                log(6, 'End of workflow reached')
                shutdown(save=my.save, abnormal=False)

if __name__ == "__main__":
    main()
