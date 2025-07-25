"""Utility functions for docutils."""

from __future__ import annotations

import os
import re
import warnings
from contextlib import contextmanager, nullcontext
from copy import copy
from pathlib import Path
from typing import TYPE_CHECKING

import docutils
from docutils import nodes
from docutils.frontend import OptionParser
from docutils.io import FileOutput
from docutils.parsers.rst import Directive, directives, roles
from docutils.readers import standalone
from docutils.statemachine import StateMachine
from docutils.transforms.references import DanglingReferences
from docutils.utils import Reporter, unescape

from sphinx.errors import SphinxError
from sphinx.locale import __
from sphinx.transforms import SphinxTransformer
from sphinx.util import logging, rst
from sphinx.util.parsing import nested_parse_to_nodes

logger = logging.getLogger(__name__)
report_re = re.compile(
    '^(.+?:(?:\\d+)?): \\((DEBUG|INFO|WARNING|ERROR|SEVERE)/(\\d+)?\\) '
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence
    from types import ModuleType, TracebackType
    from typing import Any, Protocol

    from docutils import Component
    from docutils.frontend import Values
    from docutils.nodes import Element, Node, system_message
    from docutils.parsers import Parser
    from docutils.parsers.rst.states import Inliner
    from docutils.statemachine import State, StringList
    from docutils.transforms import Transform

    from sphinx.builders import Builder
    from sphinx.config import Config
    from sphinx.environment import BuildEnvironment
    from sphinx.events import EventManager
    from sphinx.util.typing import RoleFunction

    class _LanguageModule(Protocol):
        labels: dict[str, str]
        author_separators: list[str]
        bibliographic_fields: list[str]

    class _DirectivesDispatcher(Protocol):
        def __call__(
            self,
            directive_name: str,
            language_module: _LanguageModule,
            document: nodes.document,
            /,
        ) -> tuple[type[Directive] | None, list[system_message]]: ...

    class _RolesDispatcher(Protocol):
        def __call__(
            self,
            role_name: str,
            language_module: _LanguageModule,
            lineno: int,
            reporter: Reporter,
            /,
        ) -> tuple[RoleFunction | None, list[system_message]]: ...


_READER_TRANSFORMS = [
    transform
    for transform in standalone.Reader().get_transforms()
    if transform is not DanglingReferences
]


additional_nodes: set[type[Element]] = set()


@contextmanager
def docutils_namespace() -> Iterator[None]:
    """Create namespace for reST parsers."""
    try:
        _directives = copy(directives._directives)  # type: ignore[attr-defined]
        _roles = copy(roles._roles)  # type: ignore[attr-defined]

        yield
    finally:
        directives._directives = _directives  # type: ignore[attr-defined]
        roles._roles = _roles  # type: ignore[attr-defined]

        for node in list(additional_nodes):
            unregister_node(node)
            additional_nodes.discard(node)


def is_directive_registered(name: str) -> bool:
    """Check the *name* directive is already registered."""
    return name in directives._directives  # type: ignore[attr-defined]


def register_directive(name: str, directive: type[Directive]) -> None:
    """Register a directive to docutils.

    This modifies global state of docutils.  So it is better to use this
    inside ``docutils_namespace()`` to prevent side-effects.
    """
    directives.register_directive(name, directive)


def is_role_registered(name: str) -> bool:
    """Check the *name* role is already registered."""
    return name in roles._roles  # type: ignore[attr-defined]


def register_role(name: str, role: RoleFunction) -> None:
    """Register a role to docutils.

    This modifies global state of docutils.  So it is better to use this
    inside ``docutils_namespace()`` to prevent side-effects.
    """
    roles.register_local_role(name, role)  # type: ignore[arg-type]


def unregister_role(name: str) -> None:
    """Unregister a role from docutils."""
    roles._roles.pop(name, None)  # type: ignore[attr-defined]


def is_node_registered(node: type[Element]) -> bool:
    """Check the *node* is already registered."""
    return hasattr(nodes.GenericNodeVisitor, 'visit_' + node.__name__)


def register_node(node: type[Element]) -> None:
    """Register a node to docutils.

    This modifies global state of some visitors.  So it is better to use this
    inside ``docutils_namespace()`` to prevent side-effects.
    """
    if not hasattr(nodes.GenericNodeVisitor, 'visit_' + node.__name__):
        nodes._add_node_class_names([node.__name__])  # type: ignore[attr-defined]
        additional_nodes.add(node)


def unregister_node(node: type[Element]) -> None:
    """Unregister a node from docutils.

    This is inverse of ``nodes._add_nodes_class_names()``.
    """
    if hasattr(nodes.GenericNodeVisitor, 'visit_' + node.__name__):
        delattr(nodes.GenericNodeVisitor, 'visit_' + node.__name__)
        delattr(nodes.GenericNodeVisitor, 'depart_' + node.__name__)
        delattr(nodes.SparseNodeVisitor, 'visit_' + node.__name__)
        delattr(nodes.SparseNodeVisitor, 'depart_' + node.__name__)


@contextmanager
def patched_get_language() -> Iterator[None]:
    """Patch docutils.languages.get_language() temporarily.

    This ignores the second argument ``reporter`` to suppress warnings.
    See: https://github.com/sphinx-doc/sphinx/issues/3788
    """
    from docutils.languages import get_language

    def patched_get_language(
        language_code: str, reporter: Reporter | None = None
    ) -> Any:
        return get_language(language_code)

    try:
        docutils.languages.get_language = patched_get_language  # type: ignore[assignment]
        yield
    finally:
        # restore original implementations
        docutils.languages.get_language = get_language


@contextmanager
def patched_rst_get_language() -> Iterator[None]:
    """Patch docutils.parsers.rst.languages.get_language().
    Starting from docutils 0.17, get_language() in ``rst.languages``
    also has a reporter, which needs to be disabled temporarily.

    This should also work for old versions of docutils,
    because reporter is none by default.

    See: https://github.com/sphinx-doc/sphinx/issues/10179
    """
    from docutils.parsers.rst.languages import get_language

    def patched_get_language(
        language_code: str, reporter: Reporter | None = None
    ) -> Any:
        return get_language(language_code)

    try:
        docutils.parsers.rst.languages.get_language = patched_get_language  # type: ignore[assignment]
        yield
    finally:
        # restore original implementations
        docutils.parsers.rst.languages.get_language = get_language


@contextmanager
def using_user_docutils_conf(confdir: str | os.PathLike[str] | None) -> Iterator[None]:
    """Let docutils know the location of ``docutils.conf`` for Sphinx."""
    try:
        docutils_config = os.environ.get('DOCUTILSCONFIG', None)
        if confdir:
            docutils_conf_path = Path(confdir, 'docutils.conf').resolve()
            os.environ['DOCUTILSCONFIG'] = str(docutils_conf_path)
        yield
    finally:
        if docutils_config is None:
            os.environ.pop('DOCUTILSCONFIG', None)
        else:
            os.environ['DOCUTILSCONFIG'] = docutils_config


@contextmanager
def patch_docutils(confdir: str | os.PathLike[str] | None = None) -> Iterator[None]:
    """Patch to docutils temporarily."""
    with (
        patched_get_language(),
        patched_rst_get_language(),
        using_user_docutils_conf(confdir),
    ):
        yield


class CustomReSTDispatcher:
    """Custom reST's mark-up dispatcher.

    This replaces docutils's directives and roles dispatch mechanism for reST parser
    by original one temporarily.
    """

    def __init__(self) -> None:
        self.directive_func: _DirectivesDispatcher = lambda *args: (None, [])
        self.roles_func: _RolesDispatcher = lambda *args: (None, [])

    def __enter__(self) -> None:
        self.enable()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.disable()

    def enable(self) -> None:
        self.directive_func = directives.directive
        self.role_func = roles.role

        directives.directive = self.directive  # type: ignore[assignment]
        roles.role = self.role  # type: ignore[assignment]

    def disable(self) -> None:
        directives.directive = self.directive_func  # type: ignore[assignment]
        roles.role = self.role_func

    def directive(
        self,
        directive_name: str,
        language_module: ModuleType,
        document: nodes.document,
    ) -> tuple[type[Directive] | None, list[system_message]]:
        return self.directive_func(directive_name, language_module, document)

    def role(
        self,
        role_name: str,
        language_module: ModuleType,
        lineno: int,
        reporter: Reporter,
    ) -> tuple[RoleFunction, list[system_message]]:
        return self.role_func(
            role_name,
            language_module,  # type: ignore[return-value]
            lineno,
            reporter,
        )


class ElementLookupError(Exception):
    pass


class sphinx_domains(CustomReSTDispatcher):
    """Monkey-patch directive and role dispatch, so that domain-specific
    markup takes precedence.
    """

    def __init__(self, env: BuildEnvironment) -> None:
        self.domains = env.domains
        self.current_document = env.current_document
        super().__init__()

    def directive(
        self,
        directive_name: str,
        language_module: ModuleType,
        document: nodes.document,
    ) -> tuple[type[Directive] | None, list[system_message]]:
        """Lookup a directive, given its name which can include a domain."""
        directive_name = directive_name.lower()
        # explicit domain given?
        if ':' in directive_name:
            domain_name, _, name = directive_name.partition(':')
            try:
                domain = self.domains[domain_name]
            except KeyError:
                logger.warning(__('unknown directive name: %s'), directive_name)
            else:
                element = domain.directive(name)
                if element is not None:
                    return element, []
        # else look in the default domain
        else:
            name = directive_name
            default_domain = self.current_document.default_domain
            if default_domain is not None:
                element = default_domain.directive(name)
                if element is not None:
                    return element, []

        # always look in the std domain
        element = self.domains.standard_domain.directive(name)
        if element is not None:
            return element, []

        return super().directive(directive_name, language_module, document)

    def role(
        self,
        role_name: str,
        language_module: ModuleType,
        lineno: int,
        reporter: Reporter,
    ) -> tuple[RoleFunction, list[system_message]]:
        """Lookup a role, given its name which can include a domain."""
        role_name = role_name.lower()
        # explicit domain given?
        if ':' in role_name:
            domain_name, _, name = role_name.partition(':')
            try:
                domain = self.domains[domain_name]
            except KeyError:
                logger.warning(__('unknown role name: %s'), role_name)
            else:
                element = domain.role(name)
                if element is not None:
                    return element, []
        # else look in the default domain
        else:
            name = role_name
            default_domain = self.current_document.default_domain
            if default_domain is not None:
                element = default_domain.role(name)
                if element is not None:
                    return element, []

        # always look in the std domain
        element = self.domains.standard_domain.role(name)
        if element is not None:
            return element, []

        return super().role(role_name, language_module, lineno, reporter)


class WarningStream:
    def write(self, text: str) -> None:
        matched = report_re.search(text)
        if not matched:
            logger.warning(text.rstrip('\r\n'), type='docutils')
        else:
            location, type, _level = matched.groups()
            message = report_re.sub('', text).rstrip()
            logger.log(type, message, location=location, type='docutils')


class LoggingReporter(Reporter):
    @classmethod
    def from_reporter(
        cls: type[LoggingReporter], reporter: Reporter
    ) -> LoggingReporter:
        """Create an instance of LoggingReporter from other reporter object."""
        return cls(
            reporter.source,
            reporter.report_level,
            reporter.halt_level,
            reporter.debug_flag,
            reporter.error_handler,
        )

    def __init__(
        self,
        source: str,
        report_level: int = Reporter.WARNING_LEVEL,
        halt_level: int = Reporter.SEVERE_LEVEL,
        debug: bool = False,
        error_handler: str = 'backslashreplace',
    ) -> None:
        stream = WarningStream()
        super().__init__(
            source, report_level, halt_level, stream, debug, error_handler=error_handler
        )


class NullReporter(Reporter):
    """A dummy reporter; write nothing."""

    def __init__(self) -> None:
        super().__init__('', 999, 4)


@contextmanager
def switch_source_input(state: State[list[str]], content: StringList) -> Iterator[None]:
    """Switch current source input of state temporarily."""
    try:
        # remember the original ``get_source_and_line()`` method
        gsal = state.memo.reporter.get_source_and_line  # type: ignore[attr-defined]

        # replace it by new one
        state_machine: StateMachine[None] = StateMachine([], None)  # type: ignore[arg-type]
        state_machine.input_lines = content
        state.memo.reporter.get_source_and_line = state_machine.get_source_and_line  # type: ignore[attr-defined]

        yield
    finally:
        # restore the method
        state.memo.reporter.get_source_and_line = gsal  # type: ignore[attr-defined]


class SphinxFileOutput(FileOutput):
    """Better FileOutput class for Sphinx."""

    def __init__(self, **kwargs: Any) -> None:
        self.overwrite_if_changed = kwargs.pop('overwrite_if_changed', False)
        kwargs.setdefault('encoding', 'utf-8')
        super().__init__(**kwargs)

    def write(self, data: str) -> str:
        if (
            self.destination_path
            and self.autoclose
            and 'b' not in self.mode
            and self.overwrite_if_changed
            and os.path.exists(self.destination_path)
        ):
            with open(self.destination_path, encoding=self.encoding) as f:
                on_disk = f.read()
            # skip writing: content not changed
            if on_disk == data:
                return data

        return super().write(data)


class SphinxDirective(Directive):
    """A base class for Sphinx directives.

    This class provides helper methods for Sphinx directives.

    .. versionadded:: 1.8

    .. note:: The subclasses of this class might not work with docutils.
              This class is strongly coupled with Sphinx.
    """

    @property
    def env(self) -> BuildEnvironment:
        """Reference to the :class:`.BuildEnvironment` object.

        .. versionadded:: 1.8
        """
        return self.state.document.settings.env

    @property
    def config(self) -> Config:
        """Reference to the :class:`.Config` object.

        .. versionadded:: 1.8
        """
        return self.env.config

    def get_source_info(self) -> tuple[str, int]:
        """Get source and line number.

        .. versionadded:: 3.0
        """
        return self.state_machine.get_source_and_line(self.lineno)

    def set_source_info(self, node: Node) -> None:
        """Set source and line number to the node.

        .. versionadded:: 2.1
        """
        node.source, node.line = self.get_source_info()

    def get_location(self) -> str:
        """Get current location info for logging.

        .. versionadded:: 4.2
        """
        source, line = self.get_source_info()
        if source and line:
            return f'{source}:{line}'
        if source:
            return f'{source}:'
        if line:
            return f'<unknown>:{line}'
        return ''

    def parse_content_to_nodes(
        self, allow_section_headings: bool = False
    ) -> list[Node]:
        """Parse the directive's content into nodes.

        :param allow_section_headings:
            Are titles (sections) allowed in the directive's content?
            Note that this option bypasses Docutils' usual checks on
            doctree structure, and misuse of this option can lead to
            an incoherent doctree. In Docutils, section nodes should
            only be children of ``Structural`` nodes, which includes
            ``document``, ``section``, and ``sidebar`` nodes.

        .. versionadded:: 7.4
        """
        return nested_parse_to_nodes(
            self.state,
            self.content,
            offset=self.content_offset,
            allow_section_headings=allow_section_headings,
        )

    def parse_text_to_nodes(
        self,
        text: str = '',
        /,
        *,
        offset: int = -1,
        allow_section_headings: bool = False,
    ) -> list[Node]:
        """Parse *text* into nodes.

        :param text:
            Text, in string form. ``StringList`` is also accepted.
        :param allow_section_headings:
            Are titles (sections) allowed in *text*?
            Note that this option bypasses Docutils' usual checks on
            doctree structure, and misuse of this option can lead to
            an incoherent doctree. In Docutils, section nodes should
            only be children of ``Structural`` nodes, which includes
            ``document``, ``section``, and ``sidebar`` nodes.
        :param offset:
            The offset of the content.

        .. versionadded:: 7.4
        """
        if offset == -1:
            offset = self.content_offset
        return nested_parse_to_nodes(
            self.state,
            text,
            offset=offset,
            allow_section_headings=allow_section_headings,
        )

    def parse_inline(
        self, text: str, *, lineno: int = -1
    ) -> tuple[list[Node], list[system_message]]:
        """Parse *text* as inline elements.

        :param text:
            The text to parse, which should be a single line or paragraph.
            This cannot contain any structural elements (headings,
            transitions, directives, etc).
        :param lineno:
            The line number where the interpreted text begins.
        :returns:
            A list of nodes (text and inline elements) and a list of system_messages.

        .. versionadded:: 7.4
        """
        if lineno == -1:
            lineno = self.lineno
        return self.state.inline_text(text, lineno)


class SphinxRole:
    """A base class for Sphinx roles.

    This class provides helper methods for Sphinx roles.

    .. versionadded:: 2.0

    .. note:: The subclasses of this class might not work with docutils.
              This class is strongly coupled with Sphinx.
    """

    # fmt: off
    name: str         #: The role name actually used in the document.
    rawtext: str      #: A string containing the entire interpreted text input.
    text: str         #: The interpreted text content.
    lineno: int       #: The line number where the interpreted text begins.
    inliner: Inliner  #: The ``docutils.parsers.rst.states.Inliner`` object.
    #: A dictionary of directive options for customisation
    #: (from the "role" directive).
    options: dict[str, Any]
    #: A list of strings, the directive content for customisation
    #: (from the "role" directive).
    content: Sequence[str]
    # fmt: on

    def __call__(
        self,
        name: str,
        rawtext: str,
        text: str,
        lineno: int,
        inliner: Inliner,
        options: dict[str, Any] | None = None,
        content: Sequence[str] = (),
    ) -> tuple[list[Node], list[system_message]]:
        self.rawtext = rawtext
        self.text = unescape(text)
        self.lineno = lineno
        self.inliner = inliner
        self.options = options if options is not None else {}
        self.content = content

        # guess role type
        if name:
            self.name = name.lower()
        else:
            self.name = self.env.current_document.default_role
            if not self.name:
                self.name = self.env.config.default_role
            if not self.name:
                msg = 'cannot determine default role!'
                raise SphinxError(msg)

        return self.run()

    def run(self) -> tuple[list[Node], list[system_message]]:
        raise NotImplementedError

    @property
    def env(self) -> BuildEnvironment:
        """Reference to the :class:`.BuildEnvironment` object.

        .. versionadded:: 2.0
        """
        return self.inliner.document.settings.env

    @property
    def config(self) -> Config:
        """Reference to the :class:`.Config` object.

        .. versionadded:: 2.0
        """
        return self.env.config

    def get_source_info(self, lineno: int | None = None) -> tuple[str, int]:
        # .. versionadded:: 3.0
        if lineno is None:
            lineno = self.lineno
        return self.inliner.reporter.get_source_and_line(lineno)  # type: ignore[attr-defined]

    def set_source_info(self, node: Node, lineno: int | None = None) -> None:
        # .. versionadded:: 2.0
        node.source, node.line = self.get_source_info(lineno)

    def get_location(self) -> str:
        """Get current location info for logging.

        .. versionadded:: 4.2
        """
        source, line = self.get_source_info()
        if source and line:
            return f'{source}:{line}'
        if source:
            return f'{source}:'
        if line:
            return f'<unknown>:{line}'
        return ''


class ReferenceRole(SphinxRole):
    """A base class for reference roles.

    The reference roles can accept ``link title <target>`` style as a text for
    the role.  The parsed result; link title and target will be stored to
    ``self.title`` and ``self.target``.

    .. versionadded:: 2.0
    """

    # fmt: off
    has_explicit_title: bool    #: A boolean indicates the role has explicit title or not.
    disabled: bool              #: A boolean indicates the reference is disabled.
    title: str                  #: The link title for the interpreted text.
    target: str                 #: The link target for the interpreted text.
    # fmt: on

    # \x00 means the "<" was backslash-escaped
    explicit_title_re = re.compile(r'^(.+?)\s*(?<!\x00)<(.*?)>$', re.DOTALL)

    def __call__(
        self,
        name: str,
        rawtext: str,
        text: str,
        lineno: int,
        inliner: Inliner,
        options: dict[str, Any] | None = None,
        content: Sequence[str] = (),
    ) -> tuple[list[Node], list[system_message]]:
        if options is None:
            options = {}

        # if the first character is a bang, don't cross-reference at all
        self.disabled = text.startswith('!')

        matched = self.explicit_title_re.match(text)
        if matched:
            self.has_explicit_title = True
            self.title = unescape(matched.group(1))
            self.target = unescape(matched.group(2))
        else:
            self.has_explicit_title = False
            self.title = unescape(text)
            self.target = unescape(text)

        return super().__call__(name, rawtext, text, lineno, inliner, options, content)


class SphinxTranslator(nodes.NodeVisitor):
    """A base class for Sphinx translators.

    This class adds a support for visitor/departure method for super node class
    if visitor/departure method for node class is not found.

    It also provides helper methods for Sphinx translators.

    .. versionadded:: 2.0

    .. note:: The subclasses of this class might not work with docutils.
              This class is strongly coupled with Sphinx.
    """

    def __init__(self, document: nodes.document, builder: Builder) -> None:
        super().__init__(document)
        self.builder = builder
        self.config = builder.config
        self.settings = document.settings
        self._domains = builder.env.domains

    def dispatch_visit(self, node: Node) -> None:
        """Dispatch node to appropriate visitor method.
        The priority of visitor method is:

        1. ``self.visit_{node_class}()``
        2. ``self.visit_{super_node_class}()``
        3. ``self.unknown_visit()``
        """
        for node_class in node.__class__.__mro__:
            method = getattr(self, 'visit_%s' % node_class.__name__, None)
            if method:
                method(node)
                break
        else:
            super().dispatch_visit(node)

    def dispatch_departure(self, node: Node) -> None:
        """Dispatch node to appropriate departure method.
        The priority of departure method is:

        1. ``self.depart_{node_class}()``
        2. ``self.depart_{super_node_class}()``
        3. ``self.unknown_departure()``
        """
        for node_class in node.__class__.__mro__:
            method = getattr(self, 'depart_%s' % node_class.__name__, None)
            if method:
                method(node)
                break
        else:
            super().dispatch_departure(node)

    def unknown_visit(self, node: Node) -> None:
        logger.warning(__('unknown node type: %r'), node, location=node)


# cache a vanilla instance of nodes.document
# Used in new_document() function
__document_cache__: tuple[Values, Reporter]


def new_document(source_path: str, settings: Any = None) -> nodes.document:
    """Return a new empty document object.  This is an alternative of docutils'.

    This is a simple wrapper for ``docutils.utils.new_document()``.  It
    caches the result of docutils' and use it on second call for instantiation.
    This makes an instantiation of document nodes much faster.
    """
    global __document_cache__  # NoQA: PLW0603
    try:
        cached_settings, reporter = __document_cache__
    except NameError:
        doc = docutils.utils.new_document(source_path)
        __document_cache__ = cached_settings, reporter = doc.settings, doc.reporter

    if settings is None:
        # Make a copy of the cached settings to accelerate instantiation
        settings = copy(cached_settings)

    # Create a new instance of nodes.document using cached reporter
    document = nodes.document(settings, reporter, source=source_path)
    document.note_source(source_path, -1)
    return document


def _parse_str_to_doctree(
    content: str,
    *,
    filename: Path,
    default_role: str = '',
    default_settings: Mapping[str, Any],
    env: BuildEnvironment,
    events: EventManager | None = None,
    parser: Parser,
    transforms: Sequence[type[Transform]] = (),
) -> nodes.document:
    env.current_document._parser = parser

    # Propagate exceptions by default when used programmatically:
    defaults = {'traceback': True, **default_settings}
    settings = _get_settings(
        standalone.Reader, parser, defaults=defaults, read_config_files=True
    )
    settings._source = str(filename)

    # Create root document node
    reporter = LoggingReporter(
        source=str(filename),
        report_level=settings.report_level,
        halt_level=settings.halt_level,
        debug=settings.debug,
        error_handler=settings.error_encoding_error_handler,
    )
    document = nodes.document(settings, reporter, source=str(filename))
    document.note_source(str(filename), -1)

    # substitute transformer
    document.transformer = transformer = SphinxTransformer(document)
    transformer.add_transforms(_READER_TRANSFORMS)
    transformer.add_transforms(transforms)
    transformer.add_transforms(parser.get_transforms())

    if default_role:
        default_role_cm = rst.default_role(env.current_document.docname, default_role)
    else:
        default_role_cm = nullcontext()  # type: ignore[assignment]
    with sphinx_domains(env), default_role_cm:
        # TODO: Move the stanza below to Builder.read_doc(), within
        #       a sphinx_domains() context manager.
        #       This will require changes to IntersphinxDispatcher and/or
        #       CustomReSTDispatcher.
        if events is not None:
            # emit "source-read" event
            arg = [content]
            events.emit('source-read', env.current_document.docname, arg)
            content = arg[0]

        # parse content to abstract syntax tree
        parser.parse(content, document)
        document.current_source = document.current_line = None

        # run transforms
        transformer.apply_transforms()

    return document


def _get_settings(
    *components: Component | type[Component],
    defaults: Mapping[str, Any],
    read_config_files: bool = False,
) -> Values:
    with warnings.catch_warnings(action='ignore', category=DeprecationWarning):
        # DeprecationWarning: The frontend.OptionParser class will be replaced
        # by a subclass of argparse.ArgumentParser in Docutils 0.21 or later.
        # DeprecationWarning: The frontend.Option class will be removed
        # in Docutils 0.21 or later.
        option_parser = OptionParser(
            components=components,
            defaults=defaults,
            read_config_files=read_config_files,
        )
    return option_parser.get_default_values()  # type: ignore[return-value]
