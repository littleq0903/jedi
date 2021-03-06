"""
Helpers for the API
"""
import re
from collections import namedtuple

from jedi._compatibility import u
from jedi.evaluate.helpers import call_of_leaf
from jedi import parser
from jedi.parser import tokenize, token
from jedi.cache import time_cache


CompletionParts = namedtuple('CompletionParts', ['path', 'has_dot', 'name'])


def sorted_definitions(defs):
    # Note: `or ''` below is required because `module_path` could be
    return sorted(defs, key=lambda x: (x.module_path or '', x.line or 0, x.column or 0))


def get_on_completion_name(lines, position):
    line = lines[position[0] - 1]
    # The first step of completions is to get the name
    return re.search(
        r'(?!\d)\w+$|$', line[:position[1]]
    ).group(0)


def _get_code(code_lines, start_pos, end_pos):
    """
    :param code_start_pos: is where the code starts.
    """
    # Get relevant lines.
    lines = code_lines[start_pos[0] - 1:end_pos[0]]
    # Remove the parts at the end of the line.
    lines[-1] = lines[-1][:end_pos[1]]
    # Remove first line indentation.
    lines[0] = lines[0][start_pos[1]:]
    return '\n'.join(lines)


class OnErrorLeaf(Exception):
    @property
    def error_leaf(self):
        return self.args[0]


def get_stack_at_position(grammar, code_lines, module, pos):
    """
    Returns the possible node names (e.g. import_from, xor_test or yield_stmt).
    """
    user_stmt = module.get_statement_for_position(pos)

    if user_stmt is not None and user_stmt.type in ('indent', 'dedent'):
        code = u('')
    else:
        if user_stmt is None:
            user_stmt = module.get_leaf_for_position(pos, include_prefixes=True)
        if pos <= user_stmt.start_pos:
            try:
                leaf = user_stmt.get_previous_leaf()
            except IndexError:
                pass
            else:
                user_stmt = module.get_statement_for_position(leaf.start_pos)

        if user_stmt.type == 'error_leaf' or user_stmt.type == 'string':
            # Error leafs cannot be parsed, completion in strings is also
            # impossible.
            raise OnErrorLeaf(user_stmt)

        code = _get_code(code_lines, user_stmt.start_pos, pos)
        if code == ';':
            # ; cannot be parsed.
            code = u('')

        # Remove whitespace at the end. Necessary, because the tokenizer will parse
        # an error token (there's no new line at the end in our case). This doesn't
        # alter any truth about the valid tokens at that position.
        code = code.strip('\t ')

    class EndMarkerReached(Exception):
        pass

    def tokenize_without_endmarker(code):
        tokens = tokenize.source_tokens(code, use_exact_op_types=True)
        for token_ in tokens:
            if token_[0] == token.ENDMARKER:
                raise EndMarkerReached()
            elif token_[0] == token.DEDENT:
                # Ignore those. Error statements should not contain them, if
                # they do it's for cases where an indentation happens and
                # before the endmarker we still see them.
                pass
            else:
                yield token_

    p = parser.Parser(grammar, code, start_parsing=False)
    try:
        p.parse(tokenizer=tokenize_without_endmarker(code))
    except EndMarkerReached:
        return Stack(p.stack)


class Stack(list):
    def get_node_names(self, grammar):
        for dfa, state, (node_number, nodes) in self:
            yield grammar.number2symbol[node_number]

    def get_nodes(self):
        for dfa, state, (node_number, nodes) in self:
            for node in nodes:
                yield node


def get_possible_completion_types(grammar, stack):
    def add_results(label_index):
        try:
            grammar_labels.append(inversed_tokens[label_index])
        except KeyError:
            try:
                keywords.append(inversed_keywords[label_index])
            except KeyError:
                t, v = grammar.labels[label_index]
                assert t >= 256
                # See if it's a symbol and if we're in its first set
                inversed_keywords
                itsdfa = grammar.dfas[t]
                itsstates, itsfirst = itsdfa
                for first_label_index in itsfirst.keys():
                    add_results(first_label_index)

    inversed_keywords = dict((v, k) for k, v in grammar.keywords.items())
    inversed_tokens = dict((v, k) for k, v in grammar.tokens.items())

    keywords = []
    grammar_labels = []

    def scan_stack(index):
        dfa, state, node = stack[index]
        states, first = dfa
        arcs = states[state]

        for label_index, new_state in arcs:
            if label_index == 0:
                # An accepting state, check the stack below.
                scan_stack(index - 1)
            else:
                add_results(label_index)

    scan_stack(-1)

    return keywords, grammar_labels


def evaluate_goto_definition(evaluator, leaf):
    if leaf.type == 'name':
        # In case of a name we can just use goto_definition which does all the
        # magic itself.
        return evaluator.goto_definitions(leaf)

    node = None
    parent = leaf.parent
    if parent.type == 'atom':
        node = leaf.parent
    elif parent.type == 'trailer':
        node = call_of_leaf(leaf)

    if node is None:
        return []
    return evaluator.eval_element(node)


CallSignatureDetails = namedtuple(
    'CallSignatureDetails',
    ['bracket_leaf', 'call_index', 'keyword_name_str']
)


def _get_index_and_key(nodes, position):
    """
    Returns the amount of commas and the keyword argument string.
    """
    nodes_before = [c for c in nodes if c.start_pos < position]
    if nodes_before[-1].type == 'arglist':
        nodes_before = [c for c in nodes_before[-1].children if c.start_pos < position]

    key_str = None

    if nodes_before:
        last = nodes_before[-1]
        if last.type == 'argument' and last.children[1].end_pos <= position:
            # Checked if the argument
            key_str = last.children[0].value
        elif last == '=':
            key_str = nodes_before[-2].value

    return nodes_before.count(','), key_str


def _get_call_signature_details_from_error_node(node, position):
    for index, element in reversed(list(enumerate(node.children))):
        # `index > 0` means that it's a trailer and not an atom.
        if element == '(' and element.end_pos <= position and index > 0:
            # It's an error node, we don't want to match too much, just
            # until the parentheses is enough.
            children = node.children[index:]
            name = element.get_previous_leaf()
            if name.type == 'name' or name.parent.type in ('trailer', 'atom'):
                return CallSignatureDetails(
                    element,
                    *_get_index_and_key(children, position)
                )


def get_call_signature_details(module, position):
    leaf = module.get_leaf_for_position(position, include_prefixes=True)
    if leaf == ')':
        if leaf.end_pos == position:
            leaf = leaf.get_next_leaf()
    # Now that we know where we are in the syntax tree, we start to look at
    # parents for possible function definitions.
    node = leaf.parent
    while node is not None:
        if node.type in ('funcdef', 'classdef'):
            # Don't show call signatures if there's stuff before it that just
            # makes it feel strange to have a call signature.
            return None

        for n in node.children:
            if n.start_pos < position and n.type == 'error_node':
                result = _get_call_signature_details_from_error_node(n, position)
                if result is not None:
                    return result

        if node.type == 'trailer' and node.children[0] == '(':
            leaf = node.get_previous_leaf()
            return CallSignatureDetails(
                node.children[0], *_get_index_and_key(node.children, position))

        node = node.parent

    return None


@time_cache("call_signatures_validity")
def cache_call_signatures(evaluator, bracket_leaf, code_lines, user_pos):
    """This function calculates the cache key."""
    index = user_pos[0] - 1

    before_cursor = code_lines[index][:user_pos[1]]
    other_lines = code_lines[bracket_leaf.start_pos[0]:index]
    whole = '\n'.join(other_lines + [before_cursor])
    before_bracket = re.match(r'.*\(', whole, re.DOTALL)

    module_path = bracket_leaf.get_parent_until().path
    if module_path is None:
        yield None  # Don't cache!
    else:
        yield (module_path, before_bracket, bracket_leaf.start_pos)
    yield evaluate_goto_definition(
        evaluator,
        bracket_leaf.get_previous_leaf()
    )
