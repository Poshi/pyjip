#!/usr/bin/env python
"""Basic pipeline building blocks.

This modules provides the basic building blocks in a JIP pipeline and a way
to search and find them at run-time. The basic buiding blocks are instances
of :py:class:`Tool`. The JIP library comes with two sub-classes that can be
used to create tool implementations:

:py:class:`ScriptTool`
    This sub-class of `Tool` integrates file or script based tool
    implementations which can be served from stand-alone script files
:py:class:`PythonTool`
    In contrast to the script tool, this `Tool` extension allows to create
    `Tool` instances from other, possibly non-related, python classes. The
    easiest way to used this is with the :py:class:`jip.tools.tool` decorator,
    which allows you to take arbitrary python classes and *make* them jip
    tools.

In addition to the `Tool` implementations, this module provides the
:py:class:`Scanner` class, which is used to find tool implementations either
form disk or from an arbitrary python module. This class is supposed to be
used as a *singleton* and an configured instance is available in the main
`jip` module, exposed as `jip.scanner`. The scanner class itself is
configured either through the :py:mod:`jip.configuration`, or through
environment variables. The :py:class:`Scanner` documentation covers both
the environment variables that can be used as well as the configuration
properties.
"""
import cPickle
import copy
import inspect
from textwrap import dedent
from os import remove, getcwd, getenv, listdir
from os.path import exists, basename, dirname, abspath
import os
import sys
import types

import jip.templates
from jip.options import Options, TYPE_OUTPUT, TYPE_INPUT, Option
from jip.templates import render_template, set_global_context
from jip.utils import list_dir
from jip.logger import getLogger
import jip.profiles

log = getLogger('jip.tools')

# the pickle template to store a pyton tool
_pickel_template = """
python -c '
import sys
import cPickle
import jip
import types

jip._disable_module_search = True
source="".join([l for l in sys.stdin]).decode("base64")
tool = cPickle.loads(source)
if isinstance(tool, types.FunctionType):
    tool()
else:
    tool.run()
'<< __EOF__
%s__EOF__
"""


#########################################################
# Exceptions
#########################################################
class ValidationError(Exception):
    """Exception raised in validation steps. The exception
    carries the source tool and a message.
    """
    def __init__(self, source, message):
        self.source = source
        self.message = message

    def __repr__(self):
        import jip.cli
        return "%s: %s" % (
            jip.cli.colorize(self.source, jip.cli.RED),
            jip.cli.colorize(self.message, jip.cli.YELLOW)
        )

    def __str__(self):
        return self.__repr__()


class ToolNotFoundException(Exception):
    """Raised in case a tool is not found by the scanner"""
    pass


#########################################################
# decorators
#########################################################
class tool(object):
    """The @jip.tool decorator can be used to turn classes into valid JIP
    tools. The only mandatory parameter is the tool name. All other paramters
    are optional and allow you to delegate functionality between the actual
    :class:`jip.tool.Tool` implementation and the decorated class.
    """
    def __init__(self, name=None, inputs=None, outputs=None,
                 argparse='register', get_command=None, validate=None,
                 add_outputs=None, pipeline=None, is_done=None, cleanup=None,
                 help=None, check_files=None, ensure=None, pytool=False,
                 force_pipeline=False):
        self.name = name
        self.inputs = inputs
        self.outputs = outputs
        self.argparse = argparse
        self.add_outputs = add_outputs
        self._check_files = check_files
        self._ensure = ensure
        self._pytool = pytool
        self._force_pipeline = force_pipeline

        ################################################################
        # tool delegates
        ################################################################
        self._validate = validate if validate else "validate"
        self._is_done = is_done if is_done else "is_done"
        self._pipeline = pipeline if pipeline else "pipeline"
        self._get_command = get_command if get_command else "get_command"
        self._cleanup = cleanup if cleanup else "cleanup"
        self._help = help if help else "help"

    def __call__(self, cls):
        # check the name
        if self.name is None:
            if isinstance(cls, types.FunctionType):
                self.name = cls.func_name
            else:
                self.name = cls.__name__

        # overwrite the string representation
        if not isinstance(cls, types.FunctionType):
            cls.__repr__ = lambda x: self.name
        Scanner.registry[self.name] = PythonTool(cls, self,
                                                 self.add_outputs)
        log.debug("Registered tool from module: %s", self.name)
        return cls

    ################################################################
    # tool delegates
    ################################################################
    def __call_delegate(self, fun, wrapper, instance):
        if not callable(fun):
            name = fun
            try:
                fun = getattr(instance, name)
            except:
                # try to get the function frow main Tool implementation
                fun = getattr(Tool, name)
        if fun:
            # make sure the instance is aware of the options
            if (hasattr(fun, "__self__") and fun.__self__ is not None) or \
               (hasattr(fun, "im_self") and fun.im_self is not None):
                instance.options = wrapper.options
                instance.args = wrapper.args
                instance.ensure = wrapper.ensure
                instance.check_file = wrapper.check_file
                instance.validation_error = wrapper.validation_error
                return fun()
            else:
                return fun(wrapper)

    def validate(self, wrapper, instance):
        try:
            r = self.__call_delegate(self._validate, wrapper, instance)
            if self._check_files:
                for check in self._check_files:
                    wrapper.check_file(check)
            if self._ensure:
                for e in self._ensure:
                    wrapper.ensure(e[0], e[1], None if len(e) < 3 else e[2])
            return r
        except Exception as err:
            if not isinstance(err, ValidationError):
                log.info("Validation error: %s", str(err), exc_info=True)
                err = ValidationError(wrapper, str(err))
            raise err

    def is_done(self, wrapper, instance):
        return self.__call_delegate(self._is_done, wrapper, instance)

    def pipeline(self, wrapper, instance):
        return self.__call_delegate(self._pipeline, wrapper, instance)

    def get_command(self, wrapper, instance):
        interp = "bash"
        cmd = None
        if not isinstance(instance, types.FunctionType):
            cmds = self.__call_delegate(self._get_command, wrapper,
                                        instance)
        else:
            if self._pytool:
                # this is a single function that we want to execute
                # as a tool
                wrapper.decorator.add_outputs = None
                wrapper._add_outputs = None
                wrapper.cls = None
                r = ('bash', _pickel_template %
                     (cPickle.dumps(wrapper).encode("base64")))
                return r
            else:
                # this is not a python tool function but a function
                # that will return a template
                argspec = inspect.getargspec(instance)
                if len(argspec[0]) > 0:
                    cmds = instance(wrapper)
                else:
                    cmds = instance()

        if isinstance(cmds, (list, tuple)):
            interp = cmds[0]
            cmd = cmds[1]
        else:
            cmd = cmds

        if interp and cmd:
            block = Block(content=cmd, interpreter=interp)
            return interp, block.render(wrapper)
        return None, None

    def cleanup(self, wrapper, instance):
        return self.__call_delegate(self._cleanup, wrapper, instance)

    def help(self, wrapper, instance):
        return self.__call_delegate(self._help, wrapper, instance)


class pytool(tool):
    """This is a decorator that can be used to mark single python functions
    as tools. The function will be wrapped in a PythonTool instance and
    the function must accept a single paramter self to access to tools
    options.
    """
    def __init__(self, *args, **kwargs):
        kwargs['pytool'] = True
        tool.__init__(self, *args, **kwargs)


class pipeline(tool):
    """This is a decorator that can be used to mark single python functions
    as pipelines.
    """
    def __init__(self, *args, **kwargs):
        kwargs['force_pipeline'] = True
        tool.__init__(self, *args, **kwargs)


class Scanner():
    """
    This class holds a script/tool cache
    The cache is organized in to dicts, the script_cache, which
    store name->instance pairs pointing form the name of the tool
    to its cahced instance. The find implementations will return
    clones of the instances in the cache.
    """
    registry = {}

    def __init__(self, jip_path=None, jip_modules=None):
        self.initialized = False
        self.instances = {}
        self.jip_path = jip_path if jip_path else ""
        self.jip_modules = jip_modules if jip_modules else []
        self.jip_file_paths = set([])
        self.__scanned = False
        self.__scanned_files = None

    def find(self, name, path=None, is_pipeline=False):
        if exists(name) and os.path.isfile(name):
            ## the passed argument is a file. Try to load it at a
            ## script and add the files directory to the search path
            tool = ScriptTool.from_file(name, is_pipeline=is_pipeline)
            self.instances[name] = tool
            self.jip_file_paths.add(dirname(name))
            return tool.clone()

        if not self.initialized:
            self.scan()
            self.initialized = True
        self.instances.update(Scanner.registry)
        s = name.split(" ", 1)
        args = None
        if len(s) > 1:
            import shlex
            name = s[0]
            args = shlex.split(s[1])

        tool = self.instances.get(name, None)
        if tool is None:
            tool = self.instances.get(name + ".jip", None)
        if tool is None:
            raise ToolNotFoundException("No tool named '%s' found!" % name)
        if isinstance(tool, basestring):
            ## the tool is not loaded, load the script,
            ## and add it to the cache
            tool = ScriptTool.from_file(tool, is_pipeline=is_pipeline)
            self.instances[name] = tool
        clone = tool.clone()
        if args:
            clone.parse_args(args)
        return clone

    def scan(self, path=None):
        """Searches for scripts and python modules in the configured
        locations and returns a dictionary of the detected instances

        :param path: optional path value to define a folder to scan
        :returns: dict of tools
        """
        log.debug("Searching for JIP tools")
        if self.instances is None:
            self.instances = {}
        self.scan_files(parent=path)
        self.scan_modules()
        for n, m in Scanner.registry.iteritems():
            self.instances[n] = m
        return self.instances

    def scan_files(self, parent=None):
        if parent is None and self.__scanned_files is not None:
            return self.__scanned_files
        import re
        pattern = re.compile(r'^.*(.jip)$')
        files = {}
        if parent:
            for path in self.__search(parent, pattern):
                self.instances[basename(path)] = path
                files[basename(path)] = path

        #check cwd
        for path in self.__search(getcwd(), pattern, False):
            self.instances[basename(path)] = path
            files[basename(path)] = path

        jip_path = "%s:%s" % (self.jip_path, getenv("JIP_PATH", ""))
        for folder in jip_path.split(":") + list(self.jip_file_paths):
            for path in self.__search(folder, pattern):
                self.instances[basename(path)] = path
                files[basename(path)] = path
        if parent is None:
            self.__scanned_files = files
        return files

    def __search(self, folder, pattern, recursive=True):
        log.debug("Searching folder: %s", folder)
        for path in list_dir(folder, recursive=recursive):
            if pattern.match(path) and os.path.isfile(path):
                log.debug("Found tool: %s", path)
                yield path

    def scan_modules(self):
        if self.__scanned:
            return
        path = getenv("JIP_MODULES", "")
        log.debug("Scanning modules")
        for module in path.split(":") + self.jip_modules + ['jip.scripts']:
            try:
                if module:
                    log.debug("Importing module: %s", module)
                    __import__(module)
            except ImportError, e:
                log.debug("Error while importing module: %s. "
                          "Trying file import", str(e))
                if exists(module):
                    self._load_from_file(module)
        self.__scanned = True

    def _load_from_file(self, path):
        """Try to load a module from the given file. No module is loaded
        if the file does not exists. Otherwise, a fukk module name us guessed
        by checking for __init__.py files upwards. Then imp.load_source is
        used to import the module

        :param path: the path to the module file
        """
        if not exists(path):
            return
        name, parent_dir = self._guess_module_name(path)
        log.debug("Importing module from file: %s %s %s", name, path,
                  parent_dir)
        sys.path.append(parent_dir)
        __import__(name)
        #imp.load_source(name, path)

    def _guess_module_name(self, path):
        """Guess the absolute module name for the given file by checking for
        __init__.py files in the current folder structure and upwards"""
        path = abspath(path)
        base = basename(path)
        if base.endswith('.py'):
            base = base[:-3]
        name = [base]

        def _load_package_name(current, module_name):
            inits = filter(lambda x: x == '__init__.py', listdir(current))
            if inits:
                module_name.append(basename(current))
                return _load_package_name(dirname(current), module_name)
            return module_name, current
        # check if this is in a package
        name, parent_dir = _load_package_name(dirname(path), name)
        name.reverse()
        return ".".join(name), parent_dir


class Block(object):
    """Base class for executable blocks that can render themselfes to scripts
    and provide information about the interpreter that should be used to
    run the script.
    """
    def __init__(self, content=None, interpreter=None, interpreter_args=None,
                 lineno=0):
        self._lineno = lineno
        self.interpreter = interpreter
        self._process = None
        self.content = content
        if self.content is None:
            self.content = []
        self.interpreter_args = interpreter_args

    def run(self, tool, stdin=None, stdout=None):
        """Execute this block
        """
        import subprocess
        import jip

        # write template to named temp file and run with interpreter
        script_file = jip.create_temp_file()
        try:
            script_file.write(self.render(tool))
            script_file.close()
            cmd = [self.interpreter if self.interpreter else "bash"]
            if self.interpreter_args:
                cmd += self.interpreter_args
            self.process = subprocess.Popen(
                cmd + [script_file.name],
                stdin=stdin,
                stdout=stdout
            )
            return self.process
        except OSError, err:
            # catch the errno 2 No such file or directory, which indicates the
            # interpreter is not available
            if err.errno == 2:
                raise Exception("Interpreter %s not found!" % self.interpreter)
            raise err

    def render(self, tool):
        """Execute this block
        """
        content = self.content
        if isinstance(content, (list, tuple)):
            content = "\n".join(content)
        ctx = dict(tool.options.to_dict(raw=True))
        ctx['tool'] = tool
        ctx['args'] = tool.options.to_dict()
        ctx['options'] = tool.options.to_cmd
        tool.options.render_context(ctx)

        return render_template(content, **ctx)

    def terminate(self):
        """
        Terminate currently running blocks
        """
        if self._process is not None:
            if self._process._popen is not None:
                self._process.terminate()

                import time
                # sleep and check job states a few times before we do a hard
                # kill
                for t in [0.01, 0.05, 0.10, 2, 3]:
                    time.sleep(t)
                    if not self.process.is_alive():
                        break

                if self.process.is_alive():
                    # kill it
                    import os
                    import signal
                    os.kill(self.process._popen.pid, signal.SIGKILL)

    def __str__(self):
        return "Block['%s']" % self.interpreter


class PythonBlockUtils(object):
    """Utility functions that are exposed in template blocks and template
    functions"""

    def __init__(self, tool, local_env):
        self.tool = tool
        self._pipeline = None
        self._local_env = local_env
        self._global_env = None
        if hasattr(tool, "_pipeline"):
            self._pipeline = tool._pipeline

    @property
    def pipeline(self):
        from jip import Pipeline
        if self._pipeline is None:
            self._pipeline = Pipeline()
        return self._pipeline

    def check_file(self, name):
        """Checks for the existence of a file option.

        :param name: the options name
        """
        opt = self.tool.options[name]
        if not opt.is_dependency():
            self.tool.options[name].validate()

    def set(self, name, value):
        """Set an options value"""
        self.tool.options[name].value = value

    def run(self, name, **kwargs):
        return self.pipeline.run(name, **kwargs)

    def job(self, *args, **kwargs):
        return self.pipeline.job(*args, **kwargs)

    def name(self, name):
        self.pipeline.name(name)
        self.tool._job_name = name
        #if self._pipeline is not None:
            #self._pipeline.name(name)
        #else:
            #self.tool.name = name

    def bash(self, command, **kwargs):
        from jip.pipelines import Node

        bash_node = self.pipeline.run('bash', cmd=command, **kwargs)
        # create a render context
        ctx = dict(self._global_env)
        ctx.update(kwargs)
        ## update all Nodes with their default output options

        class OptionWrapper(object):
            def __init__(self, node, option):
                self.node = node
                self.option = option

            def __str__(self):
                bash_node.depends_on(self.node)
                return str(self.option)

        for k in ctx.keys():
            v = ctx[k]
            if isinstance(v, Node):
                ctx[k] = OptionWrapper(v,
                                       v._tool.options.get_default_output())
        # add options
        #ctx['input'] = bash_node._tool.options['input']
        #ctx['output'] = bash_node._tool.options['output']
        #ctx['outfile'] = bash_node._tool.options['outfile']
        cmd = render_template(command, **ctx)
        bash_node.cmd = cmd
        return bash_node


class PythonBlock(Block):
    """Extends block and runs the content as embedded python
    """
    def __init__(self, content=None, lineno=0):
        Block.__init__(self, content=content, lineno=lineno)
        self.interpreter = "__embedded__"

    def run(self, tool, stdin=None, stdout=None):
        """Execute this block as an embedded python script
        """
        log.debug("Block: run python block for: %s", tool)
        #tmpl = self.render(tool)
        content = self.content
        if isinstance(content, (list, tuple)):
            content = "\n".join(content)
        local_env = locals()
        utils = PythonBlockUtils(tool, local_env)
        profile = jip.profiles.Profile()
        if hasattr(tool, '_job'):
            profile = tool._job

        env = {
            "tool": tool,
            "args": tool.options.to_dict(),
            "opts": tool.options,
            "check_file": utils.check_file,
            "run": utils.run,
            "bash": utils.bash,
            "job": utils.job,
            "name": utils.name,
            "add_output": tool.options.add_output,
            "add_input": tool.options.add_input,
            "add_option": tool.options.add_option,
            "set": utils.set,
            'r': render_template,
            'render_template': render_template,
            'utils': utils,
            'profile': profile,
            'basename': basename,
            'dirname': dirname,
            'abspath': abspath,
            'pwd': getcwd(),
            'exists': exists,
            '__file__': tool.path if tool.path else None
        }

        # link known tools into the context
        from jip import scanner
        from functools import partial
        scanner.scan_modules()
        for name, cls in scanner.registry.iteritems():
            if not name in env:
                env[name] = partial(utils.run, name)
        for name, path in scanner.scan_files().iteritems():
            k = name
            if k.endswith(".jip"):
                k = k[:-4]
            if not k in env:
                env[k] = partial(utils.run, name)

        # link options to context
        for o in tool.options:
            if not o.name in env:
                n = o.name.replace("-", "_").replace(" ", "_")
                env[n] = o

        utils._global_env = env
        old_global_context = jip.templates.global_context
        set_global_context(env)
        try:
            exec content in local_env, env
        except Exception as e:
            if hasattr(e, 'lineno'):
                e.lineno += self._lineno
            raise
        finally:
            set_global_context(old_global_context)

        # auto naming for tools
        from jip.pipelines import Node
        for k, v in env.iteritems():
            if isinstance(v, Node):
                if v._job.name is None:
                    v._job.name = k
        log.debug("Block: block for: %s executed", tool)
        return env

    def terminate(self):
        pass

    def __str__(self):
        return "PythonBlock"


class Tool(object):
    """The base class for all implementation of executable units.

    This class provides all the building block to integrated new tool
    implementations that can be executed, submitted and integrated in pipelines
    to construct more complex setups.

    A `Tool` in a JIP setup is considered to be a container for the executions
    meta-data, i.e. options and files that are needed to the actual run. The
    main function of the `Tool` class is it :py:meth:`get_command`
    function, which returns a tuple `(interpreter, command)`, where the
    `interpreter` is a string like "bash" or "perl" or even a *path* to some
    interpreter executable that will be used to execute the `command`. The
    command itself is the string representation of the content of a script that
    will be passed to the `interpreter` at execution time. Please note that
    the :py:meth:`get_command` functions command part is supposed to be
    fully *rendered*, it will not be modified any further. The JIP default
    tool classes that are used, for example, to provide script to the system,
    are already integrated with the :py:mod:`jip.templates` system, but you can
    easily use the rendering function directly to create more dynamic commands
    that can adopt easily to changed in the configuration of a tool.

    The class exposes a name and a path to a source file as properties. Both
    are optional and can be omitted in order to implement anonymous tools. In
    addition to these *meta* data, the tools :py:meth:`__init__` function
    allows you to provide a *options_source*. This object is used to create the
    :py:class:`jip.options.Options` that cover the runtime configuration of a
    tool.  The options are initialize lazily on first access using the
    `options_source` provided at initialization time. This object can be either
    a string or an instance of an `argparse.ArgumentParser`. Both styles of
    providing tool options are described in the :py:mod:`jip.options` module.
    """

    def __init__(self, options_source=None, name=None):
        """Initialize a tool instance. If no options_source is given
        the class docstring is used as a the options source.

        :param options_source: either a string or an argparser instance
                               defaults to the class docstring
        :param name: the name of this tool
        """
        #: the tools name
        self.name = name
        self._job_name = None
        #: path to the tools source file
        self.path = None
        self._options = None
        self._options_source = options_source

    @property
    def options(self):
        """Access this tools :py:class:`jip.options.Options` instance.

        The tools options are the main way to interact with and configure a
        tool instance either from outside or from within a pipeline.
        """
        if self._options is None:
            if self._options_source is not None:
                self._options = self._parse_options(self._options_source)
        return self._options

    @property
    def args(self):
        """Returns a dictionary from the option names to the option values
        """
        return self.options.to_dict()

    def parse_args(self, args):
        """Parses the given argument. An excetion is raised if
        an error ocurres during argument parsing

        :param args: the argument list
        :type args: list of strings
        """
        self.options.parse(args)

    def _parse_options(self, options_source, inputs=None, outputs=None):
        """Initialize the options from the docstring or an argparser.
        In addition to the options, the function tries to deduce a tool
        name if none was specified at construction time.

        Optional inputs and outputs lists can be specified. Both must
        be lists of strings containing option names. If the option is found
        the option type is set accordingly to input or output. This is
        usefull if the options are not organized in groups and the
        parser can not automatically identify the options type.

        :param options_source: ther a docstring or an argparser instance
        :type options_source: string or argparse.ArgumentParser
        :param inputs: list of option names that will be marked as inputs
        :type inputs: list of strings
        :param outputs: list of option names that will be marked as outputs
        :type outputs: list of strings
        """
        if options_source is None:
            raise Exception("No docstring or argument parser provided!")
        opts = None
        if not isinstance(options_source, basestring):
            opts = Options.from_argparse(options_source, source=self,
                                         inputs=inputs, outputs=outputs)
        else:
            opts = Options.from_docopt(options_source, source=self,
                                       inputs=inputs, outputs=outputs)
        if self.name is None:
            import re
            match = re.match(r'usage:\s*\n*(\w+).*', opts.usage(),
                             re.IGNORECASE | re.MULTILINE)
            if match:
                self.name = match.groups()[0]
        return opts

    def validate(self):
        """The default implementation validates all options that belong to
        this tool and checks that all options that are of `TYPE_INPUT`
        reference existing files.

        The method raises a :py:class:`ValidationError` in case an option could
        not be validated or an input file does not exist.
        """
        log.debug("Default options validation for %s", self)
        try:
            self.options.validate()
        except Exception, e:
            log.info("Validation error: %s", str(e), exc_info=True)
            raise ValidationError(self, str(e))

        for opt in self.options.get_by_type(TYPE_INPUT):
            if opt.source is not None and opt.source != self:
                continue
            if opt.is_dependency():
                continue
            for value in opt._value:
                if isinstance(value, basestring):
                    if not exists(value):
                        raise ValidationError(self,
                                              "Input file not found: %s" %
                                              value)

    def validation_error(self, message, *args):
        raise ValidationError(self, message % args)

    def ensure(self, option_name, check, message=None):
        """Check a given option value using the check pattern or function and
        raise a ValidationError in case the pattern does not match or the
        function does return False.

        In case of list values, please note that in case check is a pattern,
        all values are checked independently. If check is a function, the
        list is passed on as is if the option takes list values, otherwise,
        the check function is called for each value independently.

        Note also that you shoudl not use this function to check for file
        existence. Use the `check_file()` function on the option or on the
        tool instead. `check_file` checks for incoming dependencies in
        pipelines, in which case the file does not exist _yet_ but it
        will be created by a parent job.

        :param option_name: the name of the option to check
        :param check: either a string that is interpreter as a regexp pattern
                      or a function that takes the options value as a single
                      paramter and returns True if the value is valid
        """
        o = self.options[option_name]
        if isinstance(check, basestring):
            # regexp patter
            import re
            for v in o.value:
                if not re.match(check, str(v)):
                    self.validation_error(
                        message if message else "check failed for %s" % str(v)
                    )
            return
        elif callable(check):
            if o.nargs == 0 or o.nargs == 1:
                for v in o.value:
                    if not check(v):
                        self.validation_error(
                            message if message
                            else "check failed for %s" % str(v)
                        )
            else:
                if not check(o.value):
                    self.validation_error(
                        message if message else "check failed for %s" % str(v)
                    )
            return
        raise Exception("Ensure check paramter has to be a "
                        "function or a pattern")

    def check_file(self, option_name):
        """Delegates to the options check name function

        :param option_name: the name of the option
        """
        try:
            self.options[option_name].check_file()
        except ValueError as e:
            self.validation_error(str(e))

    def is_done(self):
        """The default implementation return true if the tools has output
        files and all output files exist.
        """
        outfiles = set(self.get_output_files())
        if len(outfiles) == 0:
            return False
        for outfile in outfiles:
            if not exists(outfile):
                return False
        return True

    def pipeline(self):
        """Create and return the pipeline that will run this tool"""
        return None

    def get_command(self):
        """Return a tuple of (template, interpreter) where the template is
        a string that will be rendered and the interpreter is a name of
        an interpreter that will be used to run the filled template.
        """
        return "bash", _pickel_template % \
            (cPickle.dumps(self).encode("base64"))

    def cleanup(self):
        """The celanup method removes all output files for this tool"""
        outfiles = list(self.get_output_files(sticky=False))
        log.debug("Tool cleanup check files: %s", outfiles)
        for outfile in outfiles:
            if exists(outfile):
                log.warning("Tool cleanup! Removing: %s", outfile)
                remove(outfile)

    def get_output_files(self, sticky=True):
        """Yields a list of all output files for the options
        of this tool. Only TYPE_OUTPUT options are considered
        whose values are strings. If a source for the option
        is not None, it has to be equal to this tool.

        If `sticky` is set to False, all options marked with the
        sticky flag are ignored

        :param sticky: by default all output option values are returned,
                       if this is set to False, only non-sticky output
                       options are yield
        :type sticky:  boolean
        :returns: list of option values
        """
        for opt in self.options.get_by_type(TYPE_OUTPUT):
            if (opt.source and opt.source != self) or \
               (not sticky and opt.sticky):
                continue
            values = opt.raw()
            if not isinstance(values, (list, tuple)):
                values = [values]
            for value in values:
                if isinstance(value, basestring):
                    yield value

    def get_input_files(self):
        """Yields a list of all input files for the options
        of this tool. Only TYPE_INPUT options are considered
        whose values are strings. If a source for the option
        is not None, it has to be equal to this tool.
        """
        for opt in self.options.get_by_type(TYPE_INPUT):
            if opt.source and opt.source != self:
                continue
            values = opt.raw()
            if not isinstance(values, (list, tuple)):
                values = [values]
            for value in values:
                if isinstance(value, basestring):
                    yield value

    def help(self):
        """Return help for this tool. By default this delegates
        to the options help.
        """
        return dedent(self.options.help())

    def __repr__(self):
        return self.name if self.name else "<Unknown>"

    def __str__(self):
        return self.__repr__()

    def clone(self, counter=None):
        """Clones this instance of the tool and returns the clone. If the
        optional counter is profiled, the name of the cloned tool will be
        updated using .counter as a suffix.
        """
        cloned_tool = copy.copy(self)
        cloned_tool._options = copy.deepcopy(self._options)
        if cloned_tool.name and counter is not None:
            cloned_tool.name = "%s.%d" % (cloned_tool.name, str(counter))
        cloned_tool.options._help = self.options._help
        cloned_tool.options._usage = self.options._usage
        # update the options source
        cloned_tool.options.source = cloned_tool
        for o in cloned_tool.options:
            o.source = cloned_tool
        return cloned_tool


class PythonTool(Tool):
    """An extension of the tool class that is initialized
    with a decorated class to simplify the process of implementing
    Tools in python.
    """
    def __init__(self, cls, decorator, add_outputs=None):
        """Initialize a new python tool

        :param cls: the wrapped class
        :type cls: class
        :param decorator: an instance of the :class:`jip.tool` decorator
        :type decorator: jip.tool
        :param add_outputs: list of additional names that will be added
                            to the list of output options
        """
        Tool.__init__(self)
        self.decorator = decorator
        self.cls = cls
        self.name = decorator.name
        try:
            if not isinstance(cls, types.FunctionType):
                self.instance = cls()
            else:
                self.instance = cls
        except:
            self.instance = cls
        ################################################################
        # Load options either through a argparser function that was
        # specified by name in the decorator or load them from the
        # docstring of the instance
        ################################################################
        self._options_source = None
        self._add_outputs = add_outputs

    @property
    def options(self):
        if self._options is not None:
            return self._options

        if self.decorator.argparse and hasattr(self.instance,
                                               self.decorator.argparse):
            #initialize the options from argparse
            import argparse

            class PrintDefaultsFormatter(argparse.HelpFormatter):
                def _get_help_string(self, action):
                    help = action.help
                    if '%(default)' not in action.help and \
                       '(default: ' not in action.help:
                        if action.default is not argparse.SUPPRESS:
                            defaulting_nargs = [argparse.OPTIONAL,
                                                argparse.ZERO_OR_MORE]
                            if action.option_strings or \
                               action.nargs in defaulting_nargs:
                                if isinstance(action.default, file):
                                    if action.default == sys.stdout:
                                        help += ' (default: stdout)'
                                    elif action.default == sys.stdin:
                                        help += ' (default: stdin)'
                                    elif action.default == sys.stderr:
                                        help += ' (default: stderr)'
                                    else:
                                        help += ' (default: <stream>)'
                                else:
                                    help += ' (default: %(default)s)'
                    return help

            self._options_source = argparse.ArgumentParser(
                prog=self.name,
                formatter_class=PrintDefaultsFormatter
            )
            init_parser = getattr(self.instance, self.decorator.argparse)
            init_parser(self._options_source)
        else:
            # initialize options from doc string
            import textwrap
            if self.instance.__doc__ is not None:
                self._options_source = textwrap.dedent(self.instance.__doc__)
            else:
                self._options_source = ""
        # create the options
        self._options = self._parse_options(self._options_source,
                                            inputs=self.decorator.inputs,
                                            outputs=self.decorator.outputs)
        ## add additional output arguments
        if self._add_outputs is not None:
            for arg in self._add_outputs:
                if isinstance(arg, (list, tuple)):
                    # get default value
                    arg = arg[0]
                self._options.add(Option(
                    arg,
                    option_type=TYPE_OUTPUT,
                    nargs=1,
                    hidden=True
                ))
        return self._options

    def run(self):
        self.instance.options = self.options
        self.instance.tool_instance = self
        if isinstance(self.instance, types.FunctionType):
            # check if the function takes a paramter
            argspec = inspect.getargspec(self.instance)
            if len(argspec[0]) > 0:
                self.instance(self)
            else:
                self.instance()
        else:
            self.instance()

    def validate(self):
        if self._add_outputs is not None:
            for arg in self._add_outputs:
                if isinstance(arg, (list, tuple)):
                    value = arg[1]
                    arg = arg[0]
                    if callable(value):
                        try:
                            value = value(self)
                        except Exception as err:
                            log.error("Error evaluating output value: %s",
                                      str(err), exc_info=True)
                    self.options[arg].set(value)

        r = self.decorator.validate(self, self.instance)
        Tool.validate(self)
        return r

    def is_done(self):
        return self.decorator.is_done(self, self.instance)

    def pipeline(self):
        if self.decorator._force_pipeline and isinstance(self.instance,
                                                         types.FunctionType):
            # force pipeline generation. Call the instance function
            # and check if the retrned value is a pipeline or a string
            # strings go into a pipeline block for evaluation, pipelines
            # are returned unmodified
            # check if the function takes a paramter
            argspec = inspect.getargspec(self.instance)
            r = None
            if len(argspec[0]) > 0:
                r = self.instance(self)
            else:
                r = self.instance()
            if isinstance(r, basestring):
                # create a pipeline block and evaluate it
                block = PythonBlock(r)
                e = block.run(self)
                return e['utils']._pipeline
            else:
                return r

        return self.decorator.pipeline(self, self.instance)

    def help(self):
        return self.decorator.help(self, self.instance)

    def cleanup(self):
        return self.decorator.cleanup(self, self.instance)

    def get_command(self):
        return self.decorator.get_command(self, self.instance)


class ScriptTool(Tool):
    """An extension of the tool class that is initialized
    with a docstring and operates on Blocks that can be loade
    form a script file or from string.

    If specified as initializer parameters, both the validation and the
    pipeline block will be handled with special care.
    Pipeline blocks currently can only be embedded python block. Therefore
    the interpreter has to be 'python'. Validation blocks where the
    interpreter is 'python' will be converted to embedded python blocks. This
    allows the validation process to modify the tool and its arguments during
    validation.
    """
    def __init__(self, docstring, command_block=None,
                 validation_block=None, pipeline_block=None):
        Tool.__init__(self, docstring)
        self.command_block = command_block
        self.validation_block = validation_block
        self.pipeline_block = pipeline_block
        if self.pipeline_block:
            if self.pipeline_block.interpreter is not None and \
                    self.pipeline_block.interpreter != 'python':
                raise Exception("Pipeline blocks have to be implemented in "
                                "python! Sorry about that, but its realy a "
                                "nice language :)")
            self.pipeline_block = PythonBlock(
                lineno=self.pipeline_block._lineno,
                content=self.pipeline_block.content
            )
        if self.validation_block and \
                (self.validation_block.interpreter is None or
                 self.validation_block .interpreter == 'python'):
            self.validation_block = PythonBlock(
                lineno=self.validation_block._lineno,
                content=self.validation_block.content
            )
        if not self.command_block and not self.pipeline_block:
            raise Exception("No executable or pipeline block found!")

    def pipeline(self):
        if self.pipeline_block:
            r = self.pipeline_block.run(self)
            return r['utils'].pipeline
        return Tool.pipeline(self)

    def run(self):
        if self.command_block:
            self.command_block.run(self)

    def validate(self):
        if self.validation_block:
            self.validation_block.run(self)
        Tool.validate(self)

    def get_command(self):
        if self.command_block:
            return self.command_block.interpreter, \
                self.command_block.render(self)
        return None, None

    @classmethod
    def from_string(cls, content):
        from jip.parser import load
        return load(content, script_class=cls)

    @classmethod
    def from_file(cls, path, is_pipeline=False):
        from jip.parser import loads
        s = loads(path, script_class=cls, is_pipeline=is_pipeline)
        return s
