from typing import (
    Any,
    Union,
    Mapping,
    KeysView,
    Generator,
    ItemsView,
    ValuesView,
)
from collections.abc import Iterable

from treelib import Tree
import networkx as nx

__all__ = ['LIST_LIKE', 'SIMPLE_TYPES', 'ensure_iterable', 'to_list']

SIMPLE_TYPES = (
    bytes,
    str,
    int,
    float,
    bool,
    type(None),
)

LIST_LIKE = (
    list,
    set,
    tuple,
    Generator,
    ItemsView,
    KeysView,
    Mapping,
    ValuesView,
)


def to_list(value: Any) -> list:
    """
    Ensures that ``value`` is a list.
    """

    if isinstance(value, LIST_LIKE):

        value = list(value)

    else:

        value = [value]

    return value


def ensure_iterable(value: Any) -> Iterable:
    """
    Returns iterables, except strings, wraps simple types into tuple.
    """

    return value if isinstance(value, LIST_LIKE) else (value, )


def create_tree_visualisation(inheritance_tree: Union[dict, nx.Graph]) -> str:
    """
    Creates a visualisation of the inheritance tree using treelib.
    """

    if isinstance(inheritance_tree, nx.Graph):

        inheritance_tree = nx.to_dict_of_lists(inheritance_tree)
        # unlist values
        inheritance_tree = {k: v[0] for k, v in inheritance_tree.items() if v}

    # find root node
    classes = set(inheritance_tree.keys())
    parents = set(inheritance_tree.values())
    root = list(parents - classes)

    if len(root) > 1:
        raise ValueError(
            'Inheritance tree cannot have more than one root node.'
        )
    else:
        root = root[0]

    if not root:
        # find key whose value is None
        root = list(inheritance_tree.keys())[list(inheritance_tree.values()
                                                 ).index(None)]

    tree = Tree()

    tree.create_node(root, root)

    while classes:

        for child in classes:

            parent = inheritance_tree[child]

            if parent in tree.nodes.keys() or parent == root:

                tree.create_node(child, child, parent=parent)

        for node in tree.nodes.keys():

            if node in classes:

                classes.remove(node)

    return tree
