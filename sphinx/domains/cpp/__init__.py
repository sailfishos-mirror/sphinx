"""The C++ language domain."""

from __future__ import annotations

import re
from types import NoneType
from typing import TYPE_CHECKING

from docutils import nodes
from docutils.parsers.rst import directives

from sphinx import addnodes
from sphinx.directives import ObjectDescription
from sphinx.domains import Domain, ObjType
from sphinx.domains.cpp._ast import (
    ASTDeclaration,
    ASTIdentifier,
    ASTNamespace,
    ASTNestedName,
    ASTNestedNameElement,
)
from sphinx.domains.cpp._ids import _max_id
from sphinx.domains.cpp._parser import DefinitionParser
from sphinx.domains.cpp._symbol import Symbol, _DuplicateSymbolError
from sphinx.errors import NoUri
from sphinx.locale import _, __
from sphinx.roles import XRefRole
from sphinx.transforms import SphinxTransform
from sphinx.transforms.post_transforms import ReferencesResolver
from sphinx.util import logging
from sphinx.util.cfamily import (
    DefinitionError,
    NoOldIdError,
    anon_identifier_re,
)
from sphinx.util.docfields import Field, GroupedField
from sphinx.util.docutils import SphinxDirective, SphinxRole
from sphinx.util.nodes import make_refnode

if TYPE_CHECKING:
    from collections.abc import Iterator, Set
    from typing import Any, ClassVar

    from docutils.nodes import Element, Node, TextElement, system_message

    from sphinx.addnodes import desc_signature, pending_xref
    from sphinx.application import Sphinx
    from sphinx.builders import Builder
    from sphinx.domains.cpp._symbol import LookupKey
    from sphinx.environment import BuildEnvironment
    from sphinx.util.typing import ExtensionMetadata, OptionSpec

# re-export objects for backwards compatibility
# See: https://github.com/sphinx-doc/sphinx/issues/12295
from sphinx.domains.cpp._ast import (  # NoQA: F401
    ASTAlignofExpr,
    ASTArray,
    ASTAssignmentExpr,
    ASTBase,
    ASTBaseClass,
    ASTBinOpExpr,
    ASTBooleanLiteral,
    ASTBracedInitList,
    ASTCastExpr,
    ASTCharLiteral,
    ASTClass,
    ASTCommaExpr,
    ASTConcept,
    ASTConditionalExpr,
    ASTDeclarator,
    ASTDeclaratorMemPtr,
    ASTDeclaratorNameBitField,
    ASTDeclaratorNameParamQual,
    ASTDeclaratorParamPack,
    ASTDeclaratorParen,
    ASTDeclaratorPtr,
    ASTDeclaratorRef,
    ASTDeclSpecs,
    ASTDeclSpecsSimple,
    ASTDeleteExpr,
    ASTEnum,
    ASTEnumerator,
    ASTExplicitCast,
    ASTExplicitSpec,
    ASTExpression,
    ASTFallbackExpr,
    ASTFoldExpr,
    ASTFunctionParameter,
    ASTIdExpression,
    ASTInitializer,
    ASTLiteral,
    ASTNewExpr,
    ASTNoexceptExpr,
    ASTNoexceptSpec,
    ASTNumberLiteral,
    ASTOperator,
    ASTOperatorBuildIn,
    ASTOperatorLiteral,
    ASTOperatorType,
    ASTPackExpansionExpr,
    ASTParametersQualifiers,
    ASTParenExpr,
    ASTParenExprList,
    ASTPointerLiteral,
    ASTPostfixArray,
    ASTPostfixCallExpr,
    ASTPostfixDec,
    ASTPostfixExpr,
    ASTPostfixInc,
    ASTPostfixMember,
    ASTPostfixMemberOfPointer,
    ASTPostfixOp,
    ASTRequiresClause,
    ASTSizeofExpr,
    ASTSizeofParamPack,
    ASTSizeofType,
    ASTStringLiteral,
    ASTTemplateArgConstant,
    ASTTemplateArgs,
    ASTTemplateDeclarationPrefix,
    ASTTemplateIntroduction,
    ASTTemplateIntroductionParameter,
    ASTTemplateKeyParamPackIdDefault,
    ASTTemplateParam,
    ASTTemplateParamConstrainedTypeWithInit,
    ASTTemplateParamNonType,
    ASTTemplateParams,
    ASTTemplateParamTemplateType,
    ASTTemplateParamType,
    ASTThisLiteral,
    ASTTrailingTypeSpec,
    ASTTrailingTypeSpecDecltype,
    ASTTrailingTypeSpecDecltypeAuto,
    ASTTrailingTypeSpecFundamental,
    ASTTrailingTypeSpecName,
    ASTType,
    ASTTypeId,
    ASTTypeUsing,
    ASTTypeWithInit,
    ASTUnaryOpExpr,
    ASTUnion,
    ASTUserDefinedLiteral,
)

logger = logging.getLogger(__name__)


def _make_phony_error_name() -> ASTNestedName:
    nne = ASTNestedNameElement(ASTIdentifier('PhonyNameDueToError'), None)
    return ASTNestedName([nne], [False], rooted=False)


class CPPObject(ObjectDescription[ASTDeclaration]):
    """Description of a C++ language object."""

    doc_field_types: list[Field] = [
        GroupedField(
            'template parameter',
            label=_('Template Parameters'),
            names=('tparam', 'template parameter'),
            can_collapse=True,
        ),
    ]

    option_spec: ClassVar[OptionSpec] = {
        'no-index-entry': directives.flag,
        'no-contents-entry': directives.flag,
        'no-typesetting': directives.flag,
        'noindexentry': directives.flag,
        'nocontentsentry': directives.flag,
        'tparam-line-spec': directives.flag,
        'single-line-parameter-list': directives.flag,
    }

    def _add_enumerator_to_parent(self, ast: ASTDeclaration) -> None:
        assert ast.objectType == 'enumerator'
        # find the parent, if it exists && is an enum
        #                     && it's unscoped,
        #                  then add the name to the parent scope
        symbol = ast.symbol
        assert symbol
        assert symbol.identOrOp is not None
        assert symbol.templateParams is None
        assert symbol.templateArgs is None
        parent_symbol = symbol.parent
        assert parent_symbol
        if parent_symbol.parent is None:
            # TODO: we could warn, but it is somewhat equivalent to unscoped
            # enums, without the enum
            return  # no parent
        parent_decl = parent_symbol.declaration
        if parent_decl is None:
            # the parent is not explicitly declared
            # TODO: we could warn, but it could be a style to just assume
            # enumerator parents to be scoped
            return
        if parent_decl.objectType != 'enum':
            # TODO: maybe issue a warning, enumerators in non-enums is weird,
            # but it is somewhat equivalent to unscoped enums, without the enum
            return
        if parent_decl.directiveType != 'enum':
            return

        target_symbol = parent_symbol.parent
        s = target_symbol.find_identifier(
            symbol.identOrOp,
            matchSelf=False,
            recurseInAnon=True,
            searchInSiblings=False,
        )
        if s is not None:
            # something is already declared with that name
            return
        decl_clone = symbol.declaration.clone()
        decl_clone.enumeratorScopedSymbol = symbol
        Symbol(
            parent=target_symbol,
            identOrOp=symbol.identOrOp,
            templateParams=None,
            templateArgs=None,
            declaration=decl_clone,
            docname=self.env.current_document.docname,
            line=self.get_source_info()[1],
        )

    def add_target_and_index(
        self, ast: ASTDeclaration, sig: str, signode: TextElement
    ) -> None:
        # general note: name must be lstrip(':')'ed, to remove "::"
        ids = []
        for i in range(1, _max_id + 1):
            try:
                id = ast.get_id(version=i)
                ids.append(id)
            except NoOldIdError:
                assert i < _max_id
        # let's keep the newest first
        ids.reverse()
        newest_id = ids[0]
        assert newest_id  # shouldn't be None
        if not re.compile(r'^[a-zA-Z0-9_]*$').match(newest_id):
            logger.warning(
                'Index id generation for C++ object "%s" failed, please '
                'report as bug (id=%s).',
                ast,
                newest_id,
                location=self.get_location(),
            )

        name = ast.symbol.get_full_nested_name().get_display_string().lstrip(':')
        # Add index entry, but not if it's a declaration inside a concept
        is_in_concept = False
        s = ast.symbol.parent
        while s is not None:
            decl = s.declaration
            s = s.parent
            if decl is None:
                continue
            if decl.objectType == 'concept':
                is_in_concept = True
                break
        if not is_in_concept and 'no-index-entry' not in self.options:
            stripped_name = name
            for prefix in self.config.cpp_index_common_prefix:
                if name.startswith(prefix):
                    stripped_name = stripped_name[len(prefix) :]
                    break
            index_text = self.get_index_text(stripped_name)
            self.indexnode['entries'].append((
                'single',
                index_text,
                newest_id,
                '',
                None,
            ))

        if newest_id not in self.state.document.ids:
            # if the name is not unique, the first one will win
            names = self.env.domaindata['cpp']['names']
            if name not in names:
                names[name] = ast.symbol.docname
            # always add the newest id
            assert newest_id
            signode['ids'].append(newest_id)
            # only add compatibility ids when there are no conflicts
            for id in ids[1:]:
                if not id:  # is None when the element didn't exist in that version
                    continue
                if id not in self.state.document.ids:
                    signode['ids'].append(id)
            self.state.document.note_explicit_target(signode)

    @property
    def object_type(self) -> str:
        raise NotImplementedError

    @property
    def display_object_type(self) -> str:
        return self.object_type

    def get_index_text(self, name: str) -> str:
        return _('%s (C++ %s)') % (name, self.display_object_type)

    def parse_definition(self, parser: DefinitionParser) -> ASTDeclaration:
        return parser.parse_declaration(self.object_type, self.objtype)

    def describe_signature(
        self, signode: desc_signature, ast: ASTDeclaration, options: dict[str, Any]
    ) -> None:
        ast.describe_signature(signode, 'lastIsName', self.env, options)

    def run(self) -> list[Node]:
        env = self.env
        if env.current_document.cpp_parent_symbol is None:
            root = env.domaindata['cpp']['root_symbol']
            env.current_document.cpp_parent_symbol = root
            env.ref_context['cpp:parent_key'] = root.get_lookup_key()

        # The lookup keys assume that no nested scopes exists inside overloaded functions.
        # See: https://github.com/sphinx-doc/sphinx/issues/5191
        # Example:
        # .. cpp:function:: void f(int)
        # .. cpp:function:: void f(double)
        #
        #    .. cpp:function:: void g()
        #
        #       :cpp:any:`boom`
        #
        # So we disallow any signatures inside functions.
        parent_symbol = env.current_document.cpp_parent_symbol
        parent_decl = parent_symbol.declaration
        if parent_decl is not None and parent_decl.objectType == 'function':
            msg = (
                'C++ declarations inside functions are not supported. '
                f'Parent function: {parent_symbol.get_full_nested_name()}\n'
                f'Directive name: {self.name}\nDirective arg: {self.arguments[0]}'
            )
            logger.warning(msg, location=self.get_location())
            name = _make_phony_error_name()
            symbol = parent_symbol.add_name(name)
            env.current_document.cpp_last_symbol = symbol
            return []
        # When multiple declarations are made in the same directive
        # they need to know about each other to provide symbol lookup for function parameters.
        # We use last_symbol to store the latest added declaration in a directive.
        env.current_document.cpp_last_symbol = None
        return super().run()

    def handle_signature(self, sig: str, signode: desc_signature) -> ASTDeclaration:
        parent_symbol: Symbol = self.env.current_document.cpp_parent_symbol

        max_len = (
            self.config.cpp_maximum_signature_line_length
            or self.config.maximum_signature_line_length
            or 0
        )
        signode['multi_line_parameter_list'] = (
            'single-line-parameter-list' not in self.options
            and (len(sig) > max_len > 0)
        )

        parser = DefinitionParser(sig, location=signode, config=self.config)
        try:
            ast = self.parse_definition(parser)
            parser.assert_end()
        except DefinitionError as e:
            logger.warning(e, location=signode)
            # It is easier to assume some phony name than handling the error in
            # the possibly inner declarations.
            name = _make_phony_error_name()
            symbol = parent_symbol.add_name(name)
            self.env.current_document.cpp_last_symbol = symbol
            raise ValueError from e

        try:
            symbol = parent_symbol.add_declaration(
                ast,
                docname=self.env.current_document.docname,
                line=self.get_source_info()[1],
            )
            # append the new declaration to the sibling list
            assert symbol.siblingAbove is None
            assert symbol.siblingBelow is None
            symbol.siblingAbove = self.env.current_document.cpp_last_symbol
            if symbol.siblingAbove is not None:
                assert symbol.siblingAbove.siblingBelow is None
                symbol.siblingAbove.siblingBelow = symbol
            self.env.current_document.cpp_last_symbol = symbol
        except _DuplicateSymbolError as e:
            # Assume we are actually in the old symbol,
            # instead of the newly created duplicate.
            self.env.current_document.cpp_last_symbol = e.symbol
            msg = __(
                'Duplicate C++ declaration, also defined at %s:%s.\n'
                "Declaration is '.. cpp:%s:: %s'."
            )
            logger.warning(
                msg,
                e.symbol.docname,
                e.symbol.line,
                self.display_object_type,
                sig,
                location=signode,
                type='duplicate_declaration',
                subtype='cpp',
            )

        if ast.objectType == 'enumerator':
            self._add_enumerator_to_parent(ast)

        # note: handle_signature may be called multiple time per directive,
        # if it has multiple signatures, so don't mess with the original options.
        options = dict(self.options)
        options['tparam-line-spec'] = 'tparam-line-spec' in self.options
        self.describe_signature(signode, ast, options)
        return ast

    def before_content(self) -> None:
        last_symbol: Symbol = self.env.current_document.cpp_last_symbol
        assert last_symbol
        self.oldParentSymbol = self.env.current_document.cpp_parent_symbol
        self.oldParentKey: LookupKey = self.env.ref_context['cpp:parent_key']
        self.env.current_document.cpp_parent_symbol = last_symbol
        self.env.ref_context['cpp:parent_key'] = last_symbol.get_lookup_key()
        self.env.current_document.cpp_domain_name = (
            *self.env.current_document.cpp_domain_name,
            last_symbol.identOrOp._stringify(str),
        )

    def after_content(self) -> None:
        self.env.current_document.cpp_parent_symbol = self.oldParentSymbol
        self.env.ref_context['cpp:parent_key'] = self.oldParentKey
        cpp_domain_name = self.env.current_document.cpp_domain_name
        self.env.current_document.cpp_domain_name = cpp_domain_name[:-1]

    def _object_hierarchy_parts(self, sig_node: desc_signature) -> tuple[str, ...]:
        last_symbol: Symbol = self.env.current_document.cpp_last_symbol
        return tuple(
            s.identOrOp._stringify(str)
            for s in last_symbol.get_full_nested_name().names
        )

    def _toc_entry_name(self, sig_node: desc_signature) -> str:
        if not sig_node.get('_toc_parts'):
            return ''

        config = self.config
        objtype = sig_node.parent.get('objtype')
        if config.add_function_parentheses and objtype in {'function', 'method'}:
            parens = '()'
        else:
            parens = ''
        *parents, name = sig_node['_toc_parts']
        if config.toc_object_entries_show_parents == 'domain':
            return '::'.join((
                *self.env.current_document.cpp_domain_name,
                name + parens,
            ))
        if config.toc_object_entries_show_parents == 'hide':
            return name + parens
        if config.toc_object_entries_show_parents == 'all':
            return '::'.join([*parents, name + parens])
        return ''


class CPPTypeObject(CPPObject):
    object_type = 'type'


class CPPConceptObject(CPPObject):
    object_type = 'concept'


class CPPMemberObject(CPPObject):
    object_type = 'member'


class CPPFunctionObject(CPPObject):
    object_type = 'function'

    doc_field_types = [
        *CPPObject.doc_field_types,
        GroupedField(
            'parameter',
            label=_('Parameters'),
            names=('param', 'parameter', 'arg', 'argument'),
            can_collapse=True,
        ),
        GroupedField(
            'exceptions',
            label=_('Throws'),
            rolename='expr',
            names=('throws', 'throw', 'exception'),
            can_collapse=True,
        ),
        GroupedField(
            'retval',
            label=_('Return values'),
            names=('retvals', 'retval'),
            can_collapse=True,
        ),
        Field(
            'returnvalue',
            label=_('Returns'),
            has_arg=False,
            names=('returns', 'return'),
        ),
    ]


class CPPClassObject(CPPObject):
    object_type = 'class'

    @property
    def display_object_type(self) -> str:
        # the distinction between class and struct is only cosmetic
        assert self.objtype in {'class', 'struct'}
        return self.objtype


class CPPUnionObject(CPPObject):
    object_type = 'union'


class CPPEnumObject(CPPObject):
    object_type = 'enum'


class CPPEnumeratorObject(CPPObject):
    object_type = 'enumerator'


class CPPNamespaceObject(SphinxDirective):
    """This directive is just to tell Sphinx that we're documenting stuff in
    namespace foo.
    """

    has_content = False
    required_arguments = 1
    optional_arguments = 0
    final_argument_whitespace = True
    option_spec: ClassVar[OptionSpec] = {}

    def run(self) -> list[Node]:
        root_symbol = self.env.domaindata['cpp']['root_symbol']
        if self.arguments[0].strip() in {'NULL', '0', 'nullptr'}:
            symbol = root_symbol
            stack: list[Symbol] = []
        else:
            parser = DefinitionParser(
                self.arguments[0], location=self.get_location(), config=self.config
            )
            try:
                ast = parser.parse_namespace_object()
                parser.assert_end()
            except DefinitionError as e:
                logger.warning(e, location=self.get_location())
                name = _make_phony_error_name()
                ast = ASTNamespace(name, None)
            symbol = root_symbol.add_name(ast.nestedName, ast.templatePrefix)
            stack = [symbol]
        self.env.current_document.cpp_parent_symbol = symbol
        self.env.current_document.cpp_namespace_stack = stack
        self.env.ref_context['cpp:parent_key'] = symbol.get_lookup_key()
        return []


class CPPNamespacePushObject(SphinxDirective):
    has_content = False
    required_arguments = 1
    optional_arguments = 0
    final_argument_whitespace = True
    option_spec: ClassVar[OptionSpec] = {}

    def run(self) -> list[Node]:
        if self.arguments[0].strip() in {'NULL', '0', 'nullptr'}:
            return []
        parser = DefinitionParser(
            self.arguments[0], location=self.get_location(), config=self.config
        )
        try:
            ast = parser.parse_namespace_object()
            parser.assert_end()
        except DefinitionError as e:
            logger.warning(e, location=self.get_location())
            name = _make_phony_error_name()
            ast = ASTNamespace(name, None)
        old_parent = self.env.current_document.cpp_parent_symbol
        if not old_parent:
            old_parent = self.env.domaindata['cpp']['root_symbol']
        symbol = old_parent.add_name(ast.nestedName, ast.templatePrefix)
        self.env.current_document.cpp_namespace_stack.append(symbol)
        self.env.current_document.cpp_parent_symbol = symbol
        self.env.ref_context['cpp:parent_key'] = symbol.get_lookup_key()
        return []


class CPPNamespacePopObject(SphinxDirective):
    has_content = False
    required_arguments = 0
    optional_arguments = 0
    final_argument_whitespace = True
    option_spec: ClassVar[OptionSpec] = {}

    def run(self) -> list[Node]:
        stack = self.env.current_document.cpp_namespace_stack
        if len(stack) == 0:
            logger.warning(
                'C++ namespace pop on empty stack. Defaulting to global scope.',
                location=self.get_location(),
            )
        else:
            stack.pop()
        if len(stack) > 0:
            symbol = stack[-1]
        else:
            symbol = self.env.domaindata['cpp']['root_symbol']
        self.env.current_document.cpp_parent_symbol = symbol
        self.env.ref_context['cpp:parent_key'] = symbol.get_lookup_key()
        return []


class AliasNode(nodes.Element):
    def __init__(
        self,
        sig: str,
        aliasOptions: dict[str, bool],
        env: BuildEnvironment | None = None,
        parentKey: LookupKey | None = None,
    ) -> None:
        super().__init__()
        self.sig = sig
        self.aliasOptions = aliasOptions
        self.parentKey: LookupKey
        if env is not None:
            if env.current_document.cpp_parent_symbol is None:
                root = env.domaindata['cpp']['root_symbol']
                env.current_document.cpp_parent_symbol = root
                env.ref_context['cpp:parent_key'] = root.get_lookup_key()
            self.parentKey = env.ref_context['cpp:parent_key']
        else:
            assert parentKey is not None
            self.parentKey = parentKey

    def copy(self) -> AliasNode:
        return self.__class__(
            self.sig, self.aliasOptions, env=None, parentKey=self.parentKey
        )


class AliasTransform(SphinxTransform):
    default_priority = ReferencesResolver.default_priority - 1

    def _render_symbol(
        self,
        s: Symbol,
        maxdepth: int,
        skip_this: bool,
        alias_options: dict[str, bool],
        render_options: dict[str, bool],
        document: Any,
    ) -> list[Node]:
        if maxdepth == 0:
            recurse = True
        elif maxdepth == 1:
            recurse = False
        else:
            maxdepth -= 1
            recurse = True

        nodes: list[Node] = []
        if not skip_this:
            signode = addnodes.desc_signature('', '')
            nodes.append(signode)
            s.declaration.describe_signature(
                signode, 'markName', self.env, render_options
            )

        if recurse:
            if skip_this:
                child_container: list[Node] | addnodes.desc = nodes
            else:
                content = addnodes.desc_content()
                desc = addnodes.desc()
                content.append(desc)
                desc.document = document
                desc['domain'] = 'cpp'
                # 'desctype' is a backwards compatible attribute
                desc['objtype'] = desc['desctype'] = 'alias'
                desc['no-index'] = True
                child_container = desc

            for s_child in s._children:
                if s_child.declaration is None:
                    continue
                if s_child.declaration.objectType in {'templateParam', 'functionParam'}:
                    continue
                child_nodes = self._render_symbol(
                    s_child,
                    maxdepth=maxdepth,
                    skip_this=False,
                    alias_options=alias_options,
                    render_options=render_options,
                    document=document,
                )
                child_container.extend(child_nodes)

            if not skip_this and len(desc.children) != 0:
                nodes.append(content)
        return nodes

    def apply(self, **kwargs: Any) -> None:
        for node in self.document.findall(AliasNode):
            sig = node.sig
            parent_key = node.parentKey
            try:
                parser = DefinitionParser(sig, location=node, config=self.config)
                ast, is_shorthand = parser.parse_xref_object()
                parser.assert_end()
            except DefinitionError as e:
                logger.warning(e, location=node)
                ast, is_shorthand = None, None

            if ast is None:
                # could not be parsed, so stop here
                signode = addnodes.desc_signature(sig, '')
                signode.clear()
                signode += addnodes.desc_name(sig, sig)
                node.replace_self(signode)
                continue

            root_symbol: Symbol = self.env.domains.cpp_domain.data['root_symbol']
            parent_symbol: Symbol = root_symbol.direct_lookup(parent_key)
            if not parent_symbol:
                logger.debug('Target: %s', sig)
                logger.debug('ParentKey: %s', parent_key)
                logger.debug(root_symbol.dump(1))
            assert parent_symbol  # should be there

            symbols: list[Symbol] = []
            if is_shorthand:
                assert isinstance(ast, ASTNamespace)
                ns = ast
                name = ns.nestedName
                if ns.templatePrefix:
                    template_decls = ns.templatePrefix.templates
                else:
                    template_decls = []
                symbols, _fail_reason = parent_symbol.find_name(
                    nestedName=name,
                    templateDecls=template_decls,
                    typ='any',
                    templateShorthand=True,
                    matchSelf=True,
                    recurseInAnon=True,
                    searchInSiblings=False,
                )
                if symbols is None:
                    symbols = []
            else:
                assert isinstance(ast, ASTDeclaration)
                decl = ast
                name = decl.name
                s = parent_symbol.find_declaration(
                    decl,
                    'any',
                    templateShorthand=True,
                    matchSelf=True,
                    recurseInAnon=True,
                )
                if s is not None:
                    symbols.append(s)

            symbols = [s for s in symbols if s.declaration is not None]

            if len(symbols) == 0:
                signode = addnodes.desc_signature(sig, '')
                node.append(signode)
                signode.clear()
                signode += addnodes.desc_name(sig, sig)

                logger.warning(
                    "Can not find C++ declaration for alias '%s'.", ast, location=node
                )
                node.replace_self(signode)
            else:
                nodes = []
                render_options = {
                    'tparam-line-spec': False,
                }
                for s in symbols:
                    assert s.declaration is not None
                    res = self._render_symbol(
                        s,
                        maxdepth=node.aliasOptions['maxdepth'],
                        skip_this=node.aliasOptions['noroot'],
                        alias_options=node.aliasOptions,
                        render_options=render_options,
                        document=node.document,
                    )
                    nodes.extend(res)
                node.replace_self(nodes)


class CPPAliasObject(ObjectDescription[str]):
    option_spec: ClassVar[OptionSpec] = {
        'maxdepth': directives.nonnegative_int,
        'noroot': directives.flag,
    }

    def run(self) -> list[Node]:
        """On purpose this doesn't call the ObjectDescription version, but is based on it.
        Each alias signature may expand into multiple real signatures (an overload set).
        The code is therefore based on the ObjectDescription version.
        """
        if ':' in self.name:
            self.domain, _, self.objtype = self.name.partition(':')
        else:
            self.domain, self.objtype = '', self.name

        node = addnodes.desc()
        node.document = self.state.document
        node['domain'] = self.domain
        # 'desctype' is a backwards compatible attribute
        node['objtype'] = node['desctype'] = self.objtype

        self.names: list[str] = []
        alias_options = {
            'maxdepth': self.options.get('maxdepth', 1),
            'noroot': 'noroot' in self.options,
        }
        if alias_options['noroot'] and alias_options['maxdepth'] == 1:
            logger.warning(
                'Error in C++ alias declaration.'
                " Requested 'noroot' but 'maxdepth' 1."
                ' When skipping the root declaration,'
                " need 'maxdepth' 0 for infinite or at least 2.",
                location=self.get_location(),
            )
        signatures = self.get_signatures()
        for sig in signatures:
            node.append(AliasNode(sig, alias_options, env=self.env))

        self.before_content()
        content_node = addnodes.desc_content('', *self.parse_content_to_nodes())
        node.append(content_node)
        self.env.current_document.obj_desc_name = ''
        self.after_content()
        return [node]


class CPPXRefRole(XRefRole):
    def process_link(
        self,
        env: BuildEnvironment,
        refnode: Element,
        has_explicit_title: bool,
        title: str,
        target: str,
    ) -> tuple[str, str]:
        refnode.attributes.update(env.ref_context)

        if not has_explicit_title:
            # major hax: replace anon names via simple string manipulation.
            # Can this actually fail?
            title = anon_identifier_re.sub('[anonymous]', str(title))

        if refnode['reftype'] == 'any':
            # Assume the removal part of fix_parens for :any: refs.
            # The addition part is done with the reference is resolved.
            if not has_explicit_title:
                title = title.removesuffix('()')
            target = target.removesuffix('()')
        # TODO: should this really be here?
        if not has_explicit_title:
            target = target.lstrip('~')  # only has a meaning for the title
            # if the first character is a tilde, don't display the module/class
            # parts of the contents
            if title[:1] == '~':
                title = title[1:]
                dcolon = title.rfind('::')
                if dcolon != -1:
                    title = title[dcolon + 2 :]
        return title, target


class CPPExprRole(SphinxRole):
    def __init__(self, asCode: bool) -> None:
        super().__init__()
        if asCode:
            # render the expression as inline code
            self.class_type = 'cpp-expr'
        else:
            # render the expression as inline text
            self.class_type = 'cpp-texpr'

    def run(self) -> tuple[list[Node], list[system_message]]:
        text = self.text.replace('\n', ' ')
        parser = DefinitionParser(
            text, location=self.get_location(), config=self.config
        )
        # attempt to mimic XRefRole classes, except that...
        try:
            ast = parser.parse_expression()
        except DefinitionError as ex:
            logger.warning(
                'Unparseable C++ expression: %r\n%s',
                text,
                ex,
                location=self.get_location(),
            )
            # see below
            node = addnodes.desc_inline('cpp', text, text, classes=[self.class_type])
            return [node], []
        parent_symbol = self.env.current_document.cpp_parent_symbol
        if parent_symbol is None:
            parent_symbol = self.env.domaindata['cpp']['root_symbol']
        # ...most if not all of these classes should really apply to the individual references,
        # not the container node
        signode = addnodes.desc_inline('cpp', classes=[self.class_type])
        ast.describe_signature(signode, 'markType', self.env, parent_symbol)
        return [signode], []


class CPPDomain(Domain):
    """C++ language domain.

    There are two 'object type' attributes being used::

    - Each object created from directives gets an assigned .objtype from ObjectDescription.run.
      This is simply the directive name.
    - Each declaration (see the distinction in the directives dict below) has a nested .ast of
      type ASTDeclaration. That object has .objectType which corresponds to the keys in the
      object_types dict below. They are the core different types of declarations in C++ that
      one can document.
    """

    name = 'cpp'
    label = 'C++'
    object_types = {
        'class': ObjType(_('class'), 'class', 'struct', 'identifier', 'type'),
        'union': ObjType(_('union'), 'union', 'identifier', 'type'),
        'function': ObjType(_('function'), 'func', 'identifier', 'type'),
        'member': ObjType(_('member'), 'member', 'var', 'identifier'),
        'type': ObjType(_('type'), 'identifier', 'type'),
        'concept': ObjType(_('concept'), 'concept', 'identifier'),
        'enum': ObjType(_('enum'), 'enum', 'identifier', 'type'),
        'enumerator': ObjType(_('enumerator'), 'enumerator', 'identifier'),
        # generated object types
        'functionParam': ObjType(
            _('function parameter'), 'identifier', 'member', 'var'
        ),
        'templateParam': ObjType(
            _('template parameter'),
            'identifier',
            'class',
            'struct',
            'union',
            'member',
            'var',
            'type',
        ),
    }

    directives = {
        # declarations
        'class': CPPClassObject,
        'struct': CPPClassObject,
        'union': CPPUnionObject,
        'function': CPPFunctionObject,
        'member': CPPMemberObject,
        'var': CPPMemberObject,
        'type': CPPTypeObject,
        'concept': CPPConceptObject,
        'enum': CPPEnumObject,
        'enum-struct': CPPEnumObject,
        'enum-class': CPPEnumObject,
        'enumerator': CPPEnumeratorObject,
        # scope control
        'namespace': CPPNamespaceObject,
        'namespace-push': CPPNamespacePushObject,
        'namespace-pop': CPPNamespacePopObject,
        # other
        'alias': CPPAliasObject,
    }
    roles = {
        'any': CPPXRefRole(),
        'class': CPPXRefRole(),
        'struct': CPPXRefRole(),
        'union': CPPXRefRole(),
        'func': CPPXRefRole(fix_parens=True),
        'member': CPPXRefRole(),
        'var': CPPXRefRole(),
        'type': CPPXRefRole(),
        'concept': CPPXRefRole(),
        'enum': CPPXRefRole(),
        'enumerator': CPPXRefRole(),
        'expr': CPPExprRole(asCode=True),
        'texpr': CPPExprRole(asCode=False),
    }
    initial_data = {
        'root_symbol': Symbol(None, None, None, None, None, None, None),
        'names': {},  # full name for indexing -> docname
    }

    def clear_doc(self, docname: str) -> None:
        if Symbol.debug_show_tree:
            logger.debug('clear_doc: %s', docname)
            logger.debug('\tbefore:')
            logger.debug(self.data['root_symbol'].dump(1))
            logger.debug('\tbefore end')

        root_symbol = self.data['root_symbol']
        root_symbol.clear_doc(docname)

        if Symbol.debug_show_tree:
            logger.debug('\tafter:')
            logger.debug(self.data['root_symbol'].dump(1))
            logger.debug('\tafter end')
            logger.debug('clear_doc end: %s', docname)
        for name, n_docname in list(self.data['names'].items()):
            if n_docname == docname:
                del self.data['names'][name]

    def process_doc(
        self, env: BuildEnvironment, docname: str, document: nodes.document
    ) -> None:
        if Symbol.debug_show_tree:
            logger.debug('process_doc: %s', docname)
            logger.debug(self.data['root_symbol'].dump(0))
            logger.debug('process_doc end: %s', docname)

    def process_field_xref(self, pnode: pending_xref) -> None:
        pnode.attributes.update(self.env.ref_context)

    def merge_domaindata(self, docnames: Set[str], otherdata: dict[str, Any]) -> None:
        if Symbol.debug_show_tree:
            logger.debug('merge_domaindata:')
            logger.debug('\tself:')
            logger.debug(self.data['root_symbol'].dump(1))
            logger.debug('\tself end')
            logger.debug('\tother:')
            logger.debug(otherdata['root_symbol'].dump(1))
            logger.debug('\tother end')

        self.data['root_symbol'].merge_with(
            otherdata['root_symbol'], docnames, self.env
        )
        our_names = self.data['names']
        for name, docname in otherdata['names'].items():
            if docname in docnames:
                if name not in our_names:
                    our_names[name] = docname
                # no need to warn on duplicates, the symbol merge already does that
        if Symbol.debug_show_tree:
            logger.debug('\tresult:')
            logger.debug(self.data['root_symbol'].dump(1))
            logger.debug('\tresult end')
            logger.debug('merge_domaindata end')

    def _check_type(self, typ: str, decl_typ: str) -> bool:
        if typ == 'any':
            return True
        objtypes = self.objtypes_for_role(typ)
        if objtypes:
            return decl_typ in objtypes
        logger.debug(f'Type is {typ}, declaration type is {decl_typ}')  # NoQA: G004
        raise AssertionError

    def _resolve_xref_inner(
        self,
        env: BuildEnvironment,
        fromdocname: str,
        builder: Builder,
        typ: str,
        target: str,
        node: pending_xref,
        contnode: Element,
    ) -> tuple[nodes.reference, str] | tuple[None, None]:
        # add parens again for those that could be functions
        if typ in {'any', 'func'}:
            target += '()'
        parser = DefinitionParser(target, location=node, config=env.config)
        try:
            ast, is_shorthand = parser.parse_xref_object()
        except DefinitionError as e:
            if typ in {'any', 'func'}:
                # hax on top of the paren hax to try to get correct errors
                parser2 = DefinitionParser(
                    target[:-2], location=node, config=env.config
                )
                try:
                    parser2.parse_xref_object()
                except DefinitionError as e2:
                    target = target[:-2]
                    ex = e2
                else:
                    # strange, that we don't get the error now, use the original
                    ex = e
            else:
                ex = e

            logger.warning(
                'Unparseable C++ cross-reference: %r\n%s', target, ex, location=node
            )
            return None, None
        parent_key: LookupKey | None = node.get('cpp:parent_key', None)
        root_symbol = self.data['root_symbol']
        if parent_key:
            parent_symbol: Symbol = root_symbol.direct_lookup(parent_key)
            if not parent_symbol:
                logger.debug('Target: %s', target)
                logger.debug('ParentKey: %s', parent_key.data)
                logger.debug(root_symbol.dump(1))
            assert parent_symbol  # should be there
        else:
            parent_symbol = root_symbol

        if is_shorthand:
            assert isinstance(ast, ASTNamespace)
            ns = ast
            name = ns.nestedName
            if ns.templatePrefix:
                template_decls = ns.templatePrefix.templates
            else:
                template_decls = []
            # let's be conservative with the sibling lookup for now
            search_in_siblings = (not name.rooted) and len(name.names) == 1
            symbols, fail_reason = parent_symbol.find_name(
                name,
                template_decls,
                typ,
                templateShorthand=True,
                matchSelf=True,
                recurseInAnon=True,
                searchInSiblings=search_in_siblings,
            )
            if symbols is None:
                if typ == 'identifier':
                    if fail_reason == 'templateParamInQualified':
                        # this is an xref we created as part of a signature,
                        # so don't warn for names nested in template parameters
                        raise NoUri(str(name), typ)
                s = None
            else:
                # just refer to the arbitrarily first symbol
                s = symbols[0]
        else:
            assert isinstance(ast, ASTDeclaration)
            decl = ast
            name = decl.name
            s = parent_symbol.find_declaration(
                decl, typ, templateShorthand=True, matchSelf=True, recurseInAnon=True
            )
        if s is None or s.declaration is None:
            txt_name = str(name)
            if txt_name.startswith('std::') or txt_name == 'std':
                raise NoUri(txt_name, typ)
            return None, None

        typ = typ.removeprefix('cpp:')
        decl_typ = s.declaration.objectType

        if not self._check_type(typ, decl_typ):
            logger.warning(
                'cpp:%s targets a %s (%s).',
                typ,
                s.declaration.objectType,
                s.get_full_nested_name(),
                location=node,
            )

        declaration = s.declaration
        if is_shorthand:
            full_nested_name = s.get_full_nested_name()
            display_name = full_nested_name.get_display_string().lstrip(':')
        else:
            display_name = decl.get_display_string()
        docname = s.docname
        assert docname

        # the non-identifier refs are cross-references, which should be processed:
        # - fix parenthesis due to operator() and add_function_parentheses
        if typ != 'identifier':
            title = contnode.pop(0).astext()
            # If it's operator(), we need to add '()' if explicit function parens
            # are requested. Then the Sphinx machinery will add another pair.
            # Also, if it's an 'any' ref that resolves to a function, we need to add
            # parens as well.
            # However, if it's a non-shorthand function ref, for a function that
            # takes no arguments, then we may need to add parens again as well.
            add_paren = 0
            if (
                not node.get('refexplicit', False)
                and declaration.objectType == 'function'
            ):
                if is_shorthand:
                    # this is just the normal haxing for 'any' roles
                    if env.config.add_function_parentheses and typ == 'any':
                        add_paren += 1
                    # and now this stuff for operator()
                    if (
                        env.config.add_function_parentheses
                        and typ == 'func'
                        and title.endswith('operator()')
                    ):
                        add_paren += 1
                    if (
                        typ in {'any', 'func'}
                        and title.endswith('operator')
                        and display_name.endswith('operator()')
                    ):
                        add_paren += 1
                else:
                    # our job here is to essentially nullify add_function_parentheses
                    if env.config.add_function_parentheses:
                        if typ == 'any' and display_name.endswith('()'):
                            add_paren += 1
                        elif typ == 'func':
                            if not display_name.endswith('()'):
                                title = title.removesuffix('()')
                    else:
                        if display_name.endswith('()'):
                            add_paren += 1
            if add_paren > 0:
                title += '()' * add_paren
            # and reconstruct the title again
            contnode += nodes.Text(title)
        res = (
            make_refnode(
                builder,
                fromdocname,
                docname,
                declaration.get_newest_id(),
                contnode,
                display_name,
            ),
            declaration.objectType,
        )
        return res

    def resolve_xref(
        self,
        env: BuildEnvironment,
        fromdocname: str,
        builder: Builder,
        typ: str,
        target: str,
        node: pending_xref,
        contnode: Element,
    ) -> nodes.reference | None:
        return self._resolve_xref_inner(
            env, fromdocname, builder, typ, target, node, contnode
        )[0]

    def resolve_any_xref(
        self,
        env: BuildEnvironment,
        fromdocname: str,
        builder: Builder,
        target: str,
        node: pending_xref,
        contnode: Element,
    ) -> list[tuple[str, nodes.reference]]:
        with logging.suppress_logging():
            retnode, objtype = self._resolve_xref_inner(
                env, fromdocname, builder, 'any', target, node, contnode
            )
        if retnode:
            if objtype == 'templateParam':
                return [('cpp:templateParam', retnode)]
            else:
                return [('cpp:' + self.role_for_objtype(objtype), retnode)]
        return []

    def get_objects(self) -> Iterator[tuple[str, str, str, str, str, int]]:
        root_symbol = self.data['root_symbol']
        for symbol in root_symbol.get_all_symbols():
            if symbol.declaration is None:
                continue
            assert symbol.docname
            full_nested_name = symbol.get_full_nested_name()
            name = str(full_nested_name).lstrip(':')
            dispname = full_nested_name.get_display_string().lstrip(':')
            object_type = symbol.declaration.objectType
            docname = symbol.docname
            newest_id = symbol.declaration.get_newest_id()
            yield name, dispname, object_type, docname, newest_id, 1

    def get_full_qualified_name(self, node: Element) -> str | None:
        target = node.get('reftarget', None)
        if target is None:
            return None
        parent_key: LookupKey | None = node.get('cpp:parent_key', None)
        if parent_key is None or len(parent_key.data) <= 0:
            return None

        root_symbol = self.data['root_symbol']
        parent_symbol = root_symbol.direct_lookup(parent_key)
        parent_name = parent_symbol.get_full_nested_name()
        return f'{parent_name}::{target}'


def _init_stuff(app: Sphinx) -> None:
    Symbol.debug_lookup = app.config.cpp_debug_lookup
    Symbol.debug_show_tree = app.config.cpp_debug_show_tree
    app.config.cpp_index_common_prefix.sort(reverse=True)


def setup(app: Sphinx) -> ExtensionMetadata:
    app.add_domain(CPPDomain)
    app.add_config_value('cpp_index_common_prefix', [], 'env', types=frozenset({list}))
    app.add_config_value('cpp_id_attributes', [], 'env', types=frozenset({list, tuple}))
    app.add_config_value(
        'cpp_paren_attributes', [], 'env', types=frozenset({list, tuple})
    )
    app.add_config_value(
        'cpp_maximum_signature_line_length',
        None,
        'env',
        types=frozenset({int, NoneType}),
    )
    app.add_post_transform(AliasTransform)

    # debug stuff
    app.add_config_value('cpp_debug_lookup', False, '', types=frozenset({bool}))
    app.add_config_value('cpp_debug_show_tree', False, '', types=frozenset({bool}))

    app.connect('builder-inited', _init_stuff)

    return {
        'version': 'builtin',
        'env_version': 9,
        'parallel_read_safe': True,
        'parallel_write_safe': True,
    }
