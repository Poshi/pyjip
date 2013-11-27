#!/usr/bin/env python
"""Manage the JIP configuration.

The JIP command line tools and the JIP API load it's default configuration
from disk. Two locations are checked by default for ``jip.json`` file. The
folder that contains the ``jip`` executable, and the current users ``$HOME``
directory. If exists, the configuration next to the ``jip`` executable is
loaded first and the users configuration can extend it.

An instance of the loaded configuration is exposed in the ``jip`` main module::

    >>> import jip
    >>> assert jip.config is not None

The :class:`Config` object provides general accessors in a dictionary fashion
to the jip configuration by also allows dotted access::

    >>> import jip
    >>> assert jip.config['jip_path'] == jip.config.jip_path

"""
import collections
import logging
from os import getenv
from os.path import join, exists


log = logging.getLogger("jip.configuration")

# the default jip configuration
_configuration = {
    "db": "sqlite:///%s/.jip/jobs.db" % (getenv("HOME", "")),
    "jip_path": "",
    "jip_modules": [],
    "profiles": {
        "default": {}
    },
    "templates": {
        "variable_open": "${",
        "variable_close": "}",
    },
    "cluster": None
}

# folder that contains the jip executable
install_path = None


class Config(object):
    """Wrapper around the JIP configuration that allows
    dotted access to the configuration. Please note that
    the dotted access works if you request it as a single key::

        >>> c = Config()
        >>> print c['profiles.default']
        {}

    But it will **not** work recursively through all attribute (
    ``c.profiles.default`` will raise an exception)
    """
    def __init__(self, _config=None):
        self._config = _config

    def _init(self):
        """Loads the configuration from disk, checking next to the ``jip``
        executable and in ``$HOME/.jip/`` for a ``jip.json`` file.
        """
        # load the default configuration
        self._config = _configuration
        # load configuration from install_path if specified
        if install_path is not None:
            path = join(install_path, "jip.json")
            if exists(path):
                log.debug("Loading configuration from %s", path)
                self._config = _update(self._config, _load(path))

        # load configuration from user hom
        home = join(getenv("HOME"), ".jip")
        path = join(home, "jip.json")
        if exists(path):
            log.debug("Loading configuration from %s", path)
            self._config = _update(self._config, _load(path))

    def get(self, name, default=None):
        """Get a value from the configuration

        :param name: the key name
        :param default: default value returned if value does not exist
        :returns: value in configuration or default value
        """
        try:
            return self.__getattr__(name)
        except:
            return default

    def __getitem__(self, name):
        return self.__getattr__(name)

    @property
    def config(self):
        if self._config is None:
            self._init()
        return self._config

    def __getattr__(self, name):
        current = self.config
        for n in name.split('.'):
            current = current[n]
        return current


def _update(config, other):
    """Recursively update the given config dict with the other dict"""
    for k, v in other.iteritems():
        if isinstance(v, collections.Mapping):
            r = _update(config.get(k, {}), v)
            config[k] = r
        else:
            config[k] = other[k]
    return config


def _load(path):
    """Load configuration from given json file"""
    import json
    with open(path) as f:
        return json.load(f)
