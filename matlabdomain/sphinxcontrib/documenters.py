# -*- coding: utf-8 -*-
"""
    matlabdomain.documenters
    ~~~~~~~~~~~~~~~~~~~~~~~~

    Automatically insert docstrings for functions, classes or whole modules into
    the doctree, thus avoiding duplication between docstrings and documentation
    for those who like elaborate docstrings.

    :copyright: Copyright 2007-2013 by the Sphinx team, see AUTHORS.
    :license: BSD, see LICENSE for details.
"""

import re
import sys
import inspect
import traceback
from types import FunctionType, BuiltinFunctionType, MethodType

from docutils import nodes
from docutils.utils import assemble_option_dict
from docutils.statemachine import ViewList

from sphinx.util import rpartition, force_decode
from sphinx.locale import _
from sphinx.pycode import ModuleAnalyzer, PycodeError
from sphinx.application import ExtensionError
from sphinx.util.nodes import nested_parse_with_titles
from sphinx.util.compat import Directive
from sphinx.util.inspect import getargspec, isdescriptor, safe_getmembers, \
     safe_getattr, safe_repr, is_builtin_class_method
from sphinx.util.pycompat import base_exception, class_types
from sphinx.util.docstrings import prepare_docstring

from sphinx.ext.autodoc import py_ext_sig_re as mat_ext_sig_re
from sphinx.ext.autodoc import Documenter, members_option, bool_option, \
    ModuleDocumenter as PyModuleDocumenter, \
    ModuleLevelDocumenter as PyModuleLevelDocumenter, \
    ClassLevelDocumenter as PyClassLevelDocumenter, \
    DocstringSignatureMixin as PyDocstringSignatureMixin, \
    FunctionDocumenter as PyFunctionDocumenter, \
    ClassDocumenter as PyClassDocumenter
import os
import json
import re

from pygments.lexers import MatlabLexer
from pygments.token import Token


# create some MATLAB objects
# TODO: +packages & @class folders
# TODO: subfunctions (not nested) and private folders/functions/classes
# TODO: script files
class MatObject(object):
    """
    Base MATLAB object to which all others are subclassed.

    :param name: Name of MATLAB object.
    :type name: str

    MATLAB objects can be :class:`MatModule`, :class:`MatFunction` or
    :class:`MatClass`. :class:`MatModule` are just folders that define a psuedo
    namespace for :class:`MatFunction` and :class:`MatClass` on that path.
    :class:`MatFunction` and :class:`MatClass` must begin with either
    ``function`` or ``classdef`` keywords. A :class:`MatObject` can be anywhere
    on the path of the :class:`MatModule`; it does not need to be in the
    top-level of the :class:`MatModule`.
    """
    def __init__(self, name):
        #: name of MATLAB object
        self.name = name

    def __str__(self):
        return '<%s: "%s">' % (self.__class__.__name__, self.name)

    @staticmethod
    def parse_mfile(mfile, name, path):
        """
        Use Pygments to parse mfile to determine type: function or class.

        :param mfile: Full path of mfile.
        :type mfile: str
        :param name: Name of :class:`MatObject`.
        :type name: str
        :param path: Path of folder containing object.
        :type path: str
        """
        # use Pygments to parse mfile to determine type: function/classdef
        with open(mfile, 'r') as f:
            code = f.read()
        tks = list(MatlabLexer().get_tokens(code))  # tokens
        # assume that functions and classes always start with a keyword
        if tks[0] == (Token.Keyword, 'function'):
            return MatFunction(name, path, tks)
        elif tks[0] == (Token.Keyword, 'classdef'):
            return MatClass(name, path, tks)
        else:
            # it's a script file
            return None

    @ staticmethod
    def matlabify(fullpath):
        """
        Makes a MatObject.

        :param fullpath: Full path of object to matlabify without file extension.
        :type fullpath: str
        """
        # separate path from file/folder name
        path, name = os.path.split(fullpath)
        # folder trumps mfile with same name
        if os.path.isdir(fullpath):
            return MatModule(name, path)  # treat folder as MATLAB module
        elif os.path.isfile(fullpath + '.m'):
            mfile = fullpath + '.m'
            return MatObject.parse_mfile(mfile, name, path)
        # allow namespace to be anywhere on the path
        else:
            for root, dirs, files in os.walk(os.path.join('.', path)):
                # don't visit vcs directories
                for vcs in ['.git', '.hg', '.svn']:
                    if vcs in dirs:
                        dirs.remove(vcs)
                # only visit mfiles
                for f in tuple(files):
                    if not f.endswith('.m'):
                        files.remove(f)
                # folder trumps mfile with same name
                if name in dirs:
                    return MatModule(name, root)
                elif name + '.m' in files:
                    mfile = os.path.join(root, name) + '.m'
                    return MatObject.parse_mfile(mfile, name, path)
                # keep walking tree
            # no matching folders or mfiles
        return None


class MatModule(MatObject):
    """
    There is no concept of a *module* in MATLAB, so repurpose *module* to be
    a folder that acts like a namespace for any :class:`MatObjects` in that
    folder. Sphinx will treats objects without a namespace as builtins.

    :param name: Name of :class:`MatObject`.
    :type name: str
    :param path: Path of folder containing :class:`MatObject`.
    :type path: str
    """
    def __init__(self, name, path=None):
        super(MatModule, self).__init__(name)
        self.path = path
    def getter(self, name, *defargs):
        fullpath = os.path.join(self.path, self.name, name)
        attr = MatObject.matlabify(fullpath)
        if attr:
            return attr
        else:
            return defargs


class MatMixin(object):
    """
    Methods to comparing and manipulating tokens in :class:`MatFunction` and
    :class:`MatClass`.
    """
    def _tk_eq(self, idx, token):
        """
        Returns ``True`` if token keys are the same and values are equal.

        :param idx: Index of token in :class:`MatObject`.
        :type idx: int
        :param token: Comparison token.
        :type token: tuple
        """
        return (self.tokens[idx][0] is token[0] and
                self.tokens[idx][1] == token[1])
    def _tk_ne(self, idx, token):
        """
        Returns ``True`` if token keys are not the same or values are not equal.

        :param idx: Index of token in :class:`MatObject`.
        :type idx: int
        :param token: Comparison token.
        :type token: tuple
        """
        return (self.tokens[idx][0] is not token[0] or
                self.tokens[idx][1] != token[1])
    def _eotk(self, idx):
        """
        Returns ``True`` if end of tokens is reached.
        """
        return idx >= len(self.tokens)

    def _blanks(self, idx):
        """
        Returns number of blank text tokens.

        :param idx: Token index.
        :type idx: int
        """
        idx0 = idx  # original index
        while self._tk_eq(idx, (Token.Text, ' ')): idx += 1
        return idx - idx0  # blanks

    def _whitespace(self, idx):
        """
        Returns number of whitespaces text tokens, including blanks, newline
        and tabs.

        :param idx: Token index.
        :type idx: int
        """
        idx0 = idx  # original index
        while (self.tokens[idx][0] is Token.Text and
               self.tokens[idx][1] in [' ', '\n', '\t']):
            idx += 1
        return idx - idx0  # whitespace

    def _indent(self, idx):
        """
        Returns indentation tabs or spaces. No indentation is zero.

        :param idx: Token index.
        :type idx: int
        """
        idx0 = idx  # original index
        while (self.tokens[idx][0] is Token.Text and
               self.tokens[idx][1] in [' ', '\t']):
            idx += 1
        return idx - idx0  # indentation




class MatFunction(MatObject):
    """
    A MATLAB function.

    :param name: Name of :class:`MatObject`.
    :type name: str
    :param path: Path of folder containing :class:`MatObject`.
    :type path: str
    :param tokens: List of tokens parsed from mfile by Pygments.
    :type tokens: list
    """
    def __init__(self, name, path, tokens):
        super(MatClass, self).__init__(name)
        #: Path of folder containing :class:`MatObject`.
        self.path = path
        #: List of tokens parsed from mfile by Pygments.
        self.tokens = tokens

    def getter(self, name, *defargs):
        return defargs


class MatClass(MatMixin, MatObject):
    """
    A MATLAB class definition.

    :param name: Name of :class:`MatObject`.
    :type name: str
    :param path: Path of folder containing :class:`MatObject`.
    :type path: str
    :param tokens: List of tokens parsed from mfile by Pygments.
    :type tokens: list
    """
    #: dictionary of MATLAB class "attributes"
    # http://www.mathworks.com/help/matlab/matlab_oop/class-attributes.html
    cls_attr_types = {'Abstract': bool, 'AllowedSubclasses': list,
                      'ConstructOnLoad': bool, 'HandleCompatible': bool,
                      'Hidden': bool, 'InferiorClasses': list, 'Sealed': bool}
    prop_attr_types = {'AbortSet': bool, 'Abstract': bool, 'Access': list,
                       'Constant': bool, 'Dependent': bool, 'GetAccess': list,
                       'GetObservable': bool, 'Hidden': bool, 'SetAccess': list,
                       'SetObservable': bool, 'Transient': bool}
    def __init__(self, name, path, tokens):
        super(MatClass, self).__init__(name)
        #: Path of folder containing :class:`MatObject`.
        self.path = path
        #: List of tokens parsed from mfile by Pygments.
        self.tokens = tokens
        #: dictionary of class attributes
        self.attrs = {}
        #: list of class superclasses
        self.bases = []
        #: docstring
        self.docstring = ''
        #: dictionary of class properties
        self.properties = {}
        #: dictionary of class methods
        self.methods = {}
        # =====================================================================
        # parse tokens
        # TODO: use generator and next() instead of stepping index!
        idx = 0  # token index
        # chekc classdef keyword
        if self._tk_ne(idx, (Token.Keyword, 'classdef')):
            raise TypeError('Object is not a class. Expected a class.')
        idx += 1
        # parse classdef signature
# classdef [(Attributes [= true], Attributes [= {}}] ...)] name [< bases & ...]
# % docstring
        idx += self._blanks(idx)  # skip blanks
        # =====================================================================
        # class "attributes"
        if self._tk_eq(idx, (Token.Punctuation, '(')):
            idx += 1
            # closing parenthesis terminates attributes
            while self._tk_ne(idx, (Token.Punctuation, ')')):
                idx += self._blanks(idx)  # skip blanks
                k, cls_attr = self.tokens[idx]  # split token key, value
                if k is Token.Name and cls_attr in MatClass.cls_attr_types:
                    self.attrs[cls_attr] = []  # add attibute to dictionary
                    idx += 1
                else:
                    errmsg = 'Unexpected class attribute: "%s".' % cls_attr
                    raise Exception(errmsg)
                    # TODO: make matlab exception
                idx += self._blanks(idx)  # skip blanks
                # continue to next attribute separated by commas
                if self._tk_eq(idx, (Token.Punctuation, ',')):
                    idx += 1
                    continue
                # attribute values
                elif self._tk_eq(idx, (Token.Punctuation, '=')):
                    idx += 1
                    idx += self._blanks(idx)  # skip blanks
                    # logical value
                    k, attr_val = self.tokens[idx]  # split token key, value
                    if (k is Token.Name and attr_val in ['true', 'false']):
                        self.attrs[cls_attr] = attr_val
                        idx += 1
                    # cell array of values
                    elif self._tk_eq(idx, (Token.Punctuation, '{')):
                        idx += 1
                        while self._tk_ne(idx, (Token.Punctuation, '}')):
                            idx += self._blanks(idx)  # skip blanks
                            # concatenate attr value string
                            attr_val = ''
                            while (self._tk_ne(idx, (Token.Text, ' ')) and
                                   self._tk_ne(idx, (Token.Punctuation, ','))):
                                attr_val += self.tokens[idx][1]
                                idx += 1
                            idx += 1
                            if attr_val:
                                self.attrs[cls_attr].append(attr_val)
                        idx += 1
                    idx += self._blanks(idx)  # skip blanks
                    # continue to next attribute separated by commas
                    if self._tk_eq(idx, (Token.Punctuation, ',')):
                        idx += 1
            idx += 1  # end of class attributes
        # =====================================================================
        # classname
        idx += self._blanks(idx)  # skip blanks
        if self._tk_ne(idx, (Token.Name, self.name)):
            errmsg = 'Unexpected class name: "%s".' % self.tokens[idx][1]
            raise Exception(errmsg)
        idx += 1
        idx += self._blanks(idx)  # skip blanks
        # =====================================================================
        # super classes
        if self._tk_eq(idx, (Token.Operator, '<')):
            idx += 1
            # newline terminates superclasses
            while self._tk_ne(idx, (Token.Text, '\n')):
                idx += self._blanks(idx)  # skip blanks
                # concatenate base name
                base_name = ''
                while not self._whitespace(idx):
                    base_name += self.tokens[idx][1]
                    idx += 1
                # if newline, don't increment index
                if self._tk_ne(idx, (Token.Text, '\n')):
                    idx += 1
                if base_name:
                    self.bases.append(base_name)
                idx += self._blanks(idx)  # skip blanks
                # continue to next super class separated by &
                if self._tk_eq(idx, (Token.Operator, '&')):
                    idx += 1
            idx += 1  # end of super classes
        # newline terminates classdef signature
        elif self._tk_eq(idx, (Token.Text, '\n')):
            idx += 1  # end of classdef signature
        # =====================================================================
        # docstring
        # Must be immediately after class and indented
        indent = self._indent(idx)  # calculation indentation
        if indent:
            idx += indent
            # concatenate docstring
            while self.tokens[idx][0] is Token.Comment:
                self.docstring += self.tokens[idx][1]
                idx += 1
                # append newline to docstring
                if self._tk_eq(idx, (Token.Text, '\n')):
                    self.docstring += self.tokens[idx][1]
                    idx += 1
                # skip tab
                indent = self._indent(idx)  # calculation indentation
                if indent:
                    idx += indent
        # =====================================================================
        # # properties & methods blocks
        # # skip comments, newlines, tab and whitespace
        # while ((self.tokens[idx][0] is Token.Text and
        #        self.tokens[idx][1] in [' ', '\n', '\t']) or
        #        self.tokens[idx][0] is Token.Comment):
        #     idx += 1
        # # find properties & methods blocks
        # if (self.tokens[idx][0] is Token.Keyword and
        #     self.tokens[idx][1] in ['properties', 'methods']):
        #     idx += 1
        # # skip newlines, tab and whitespace
        # while (self.tokens[idx][0] is Token.Text and
        #        self.tokens[idx][1] in [' ', '\n', '\t']):
        #     idx += 1
        # # Token.Keyword: "end" terminates properties & methods block
        # while self._tk_ne(idx, (Token.Keyword, 'end')):
            


    def getter(self, name, *defargs):
        """
        :class:`MatClass` ``getter`` method to get attributes.
        """
        if name in self.properties:
            return self.properties[name]
        else:
            return defargs


class MatProperty(MatObject):
    pass


class MatMethod(MatObject):
    pass


class MatStaticMethod(MatObject):
    pass


class MatlabDocumenter(Documenter):
    """
    Base class for documenters of MATLAB objects.
    """
    domain = 'matlab'

    def import_object(self):
        """Import the object given by *self.modname* and *self.objpath* and set
        it as *self.object*.

        Returns True if successful, False if an error occurred.
        """
        dbg = self.env.app.debug
        if self.objpath:
            dbg('[autodoc] from %s import %s',
                self.modname, '.'.join(self.objpath))
        try:
            # make a full path out of ``self.modname`` and ``self.objpath``
            modname = self.modname.replace('.', os.sep)  # modname may have dots
            fullpath = os.path.join(modname, *self.objpath)  # objpath is a list
            dbg('[autodoc] import %s', self.modname)
            self.module = MatModule(modname)  # the folder
            self.object = MatObject.matlabify(fullpath)
            dbg('[autodoc] => %r', self.object)
            self.object_name = os.path.basename(fullpath)
            self.parent = MatObject.matlabify(os.path.dirname(fullpath))
            if self.object:
                return True
            else:
                return False
        # this used to only catch SyntaxError, ImportError and AttributeError,
        # but importing modules with side effects can raise all kinds of errors
        except Exception:
            if self.objpath:
                errmsg = 'autodoc: failed to import %s %r from module %r' % \
                         (self.objtype, '.'.join(self.objpath), self.modname)
            else:
                errmsg = 'autodoc: failed to import %s %r' % \
                         (self.objtype, self.fullname)
            errmsg += '; the following exception was raised:\n%s' % \
                      traceback.format_exc()
            dbg(errmsg)
            self.directive.warn(errmsg)
            self.env.note_reread()
            return False
