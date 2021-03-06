from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import urllib3
import base64
from pretty_json import format_json
import os
import socket
import hvac
from ansible.inventory.group import Group
from ansible.inventory.host import Host
from ansible.plugins.vars import BaseVarsPlugin
from ansible.utils.vars import combine_vars
from ansible.errors import AnsibleInternalError

DOCUMENTATION = '''
    vars: hashivault_vars
    version_added: "2.7"
    short_description: Lookup secrets/creds in Hashicorp Vault in group/domain/host precedence order
'''

urllib3.disable_warnings()  # suppress InsecureRequestWarning

# cache for vault lookups, keyed by folder
vault_cache = {}


class VarsModule(BaseVarsPlugin):
    """
    Hashicorp Vault Vars Plugin.

    Root path in vault:
        /secret/ansible/

    Precendence (applied top to bottom, so last takes precendence):
        Groups:
            /secret/ansible/groups/all
            /secret/ansible/groups/ungrouped
            /secret/ansible/groups/your_inv_item_group
            ...

        Hosts/Domains:
            /secret/ansible/{connection}/domains/com
            /secret/ansible/{connection}/domains/example.com
            /secret/ansible/{connection}/hosts/hosta.example.com
        where {connection} is ansible_connection, e.g.: "ssh", "winrm", ...

    All values retrieved from these paths are mapped as ansible variables,
    e.g. ansible_user, ansible_password, etc.

    The layered lookups are merged, with the last taking precendence over
    earlier lookups.

    Lookups to the vault are cached for the run.
    """

    def __init__(self):
        super(BaseVarsPlugin, self).__init__()

        vault_addr = "http://127.0.0.1:8200"
        if os.environ.get('VAULT_ADDR') != None:
            vault_addr = os.environ.get('VAULT_ADDR')

        vault_token = ""
        if os.environ.get('VAULT_TOKEN') != None:
            vault_token = os.environ.get('VAULT_TOKEN')

        vault_skip_verify = False
        if os.environ.get('VAULT_SKIP_VERIFY') != None:
            vault_skip_verify = os.environ.get('VAULT_SKIP_VERIFY') == '1'

        self.v_client = hvac.Client(
            url=vault_addr,
            token=vault_token,
            verify=vault_skip_verify
            )
        assert self.v_client.is_authenticated()

    # See https://stackoverflow.com/questions/319279/how-to-validate-ip-address-in-python
    def _is_valid_ipv4_address(self, address):
        """Test if address is an ipv4 address."""
        try:
            socket.inet_pton(socket.AF_INET, address)
        except AttributeError:  # no inet_pton here, sorry
            try:
                socket.inet_aton(address)
            except socket.error:
                return False
            return address.count('.') == 3
        except socket.error:  # not a valid address
            return False
        return True

    def _is_valid_ipv6_address(self, address):
        """Test if address is an ipv6 address."""
        try:
            socket.inet_pton(socket.AF_INET6, address)
        except socket.error:  # not a valid address
            return False
        return True

    def _is_valid_ip_address(self, address):
        """Test if address is an ipv4 or ipv6 address."""
        if self._is_valid_ipv4_address(address):
            return True
        return self._is_valid_ipv6_address(address)

    def _read_vault(self, folder, entity_name):
        """Read a secret from a folder in Hashicorp Vault.

        Arguments:
            folder      -- Vault folder to read
            entity_name -- Secret name to read from folder

        Returns:
            Dictionary of result data from vault
        """
        key = "%s/%s" % (folder, entity_name)

        cached_value = vault_cache.get(key)
        if cached_value != None:
            return cached_value

        result = self.v_client.read(
            path="secret/ansible/%s" % (key)
        )
        data = {}
        if result:
            data = result["data"]
        vault_cache[key] = data
        return data

    def _get_vars(self, data, entity):
        """Resolve lookup for vars from Vault.

        Arguments:
            data -- dict to accumulate vars into
            entity -- Ansible Group or Host entity to lookup for

        Returns:
            Dictionary of combined / overlayed vars values.
        """
        folder = ""
        if isinstance(entity, Group):
            folder = "groups"
        elif isinstance(entity, Host):
            # Resolve default connection details
            if entity.vars.get("ansible_port") == None:
                if entity.vars.get("ansible_connection") == None:
                    data["ansible_port"] = 22
            else:
                data["ansible_port"] = entity.vars.get("ansible_port")

            if entity.vars.get("ansible_connection") == None:
                if data["ansible_port"] == 5985 or data["ansible_port"] == 5986:
                    data["ansible_connection"] = "winrm"
                else:
                    data["ansible_connection"] = "ssh"
            else:
                data["ansible_connection"] = entity.vars.get(
                    "ansible_connection")

            folder = "%s/hosts" % (data["ansible_connection"])

            if not self._is_valid_ip_address(entity.name):
                parts = entity.name.split('.')
                if len(parts) == 1:
                    pass

                elif len(parts) > 1:
                    folder = "%s/domains" % (data["ansible_connection"])
                    # Loop lookups from domain-root to fqdn
                    parts.reverse()
                    prev_part = ""
                    for part in parts:
                        lookup_part = part + prev_part
                        if lookup_part == entity.name:
                            folder = "%s/hosts" % (data["ansible_connection"])
                        data = combine_vars(
                            data,
                            self._read_vault(folder, lookup_part)
                        )
                        prev_part = '.' + part + prev_part
                    return data
                else:
                    raise AnsibleInternalError(
                        "Failed to extract host name parts, len: %d", len(parts))

        else:
            raise AnsibleInternalError(
                "Unrecognised entity type encountered in hashivault_vars plugin: %s", type(entity))

        return combine_vars(data, self._read_vault(folder, entity.name))

    def get_vars(self, loader, path, entities):
        """Entry point called from Ansible to get vars."""
        if not isinstance(entities, list):
            entities = [entities]

        super(VarsModule, self).get_vars(loader, path, entities)

        data = {}
        for entity in entities:
            data = self._get_vars(data, entity)

        return data
