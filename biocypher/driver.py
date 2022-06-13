#!/usr/bin/env python
# -*- coding: utf-8 -*-

#
# Copyright 2021, Heidelberg University Clinic
#
# File author(s): Sebastian Lobentanzer
#                 ...
#
# Distributed under GPLv3 license, see the file `LICENSE`.
#

"""
A wrapper around the Neo4j driver which handles the DBMS connection and
provides basic management methods.
"""

from .logger import logger
logger.debug(f"Loading module {__name__}.")

import os
import re
from more_itertools import peekable
import importlib as imp
from types import GeneratorType
from typing import List

from .create import BioCypherEdge, BioCypherNode, BioCypherRelAsNode
from .translate import BiolinkAdapter, gen_translate_edges, gen_translate_nodes
from .check import MetaEdge, VersionNode, MetaNode
from .utils import pretty
from .write import BatchWriter
from .config import config
from ._driver_base import DriverBase


class Driver(DriverBase):
    """
    Manages a connection to a biocypher database.

    The connection can be defined in three ways:
        * Providing a ready ``neo4j.Driver`` instance
        * By URI and authentication data
        * By a YAML config file

    Args:
        driver (neo4j.Driver): A ``neo4j.Driver`` instance, created by,
            for example, ``neo4j.GraphDatabase.driver``.
        db_name (str): Name of the database (Neo4j graph) to use.
        db_uri (str): Protocol, host and port to access the Neo4j
            server.
        db_auth (tuple): Neo4j server authentication data: tuple of user
            name and password.
        fetch_size (int): Optional; the fetch size to use in database
            transactions.
        config_file (str): Path to a YAML config file which provides the
            URI, user name and password.
        wipe (bool): Wipe the database after connection, ensuring the
            data is loaded into an empty database.
        increment_version (bool): Whether to increase version number
            automatically and create a new BioCypher version node in the
            graph.
    """

    def __init__(
        self,
        driver=None,
        db_name=None,
        db_uri="neo4j://localhost:7687",
        db_auth=None,
        fetch_size=1000,
        config_file="/config/module_config.yaml",
        wipe=False,
        increment_version=True,
    ):

        BaseDriver.__init__(**locals())

        # get database version node ('check' module)
        # immutable variable of each instance (ie, each call from
        # the adapter to BioCypher)
        # checks for existence of graph representation and returns
        # if found, else creates new one
        self.bl_adapter = None
        self.batch_writer = None

        # if db representation node does not exist or explicitly
        # asked for wipe, create new graph representation: default
        # yaml, interactive?
        if wipe:
            self.db_meta = VersionNode(self, from_config=True)
            self.init_db()
        else:
            self.db_meta = VersionNode(self)

        if increment_version:
            # set new current version node
            self.update_meta_graph()

    def update_meta_graph(self):
        logger.info("Updating Neo4j meta graph.")
        # add version node
        self.add_biocypher_nodes(self.db_meta)

        # connect version node to previous
        if self.node_count > 1:
            e_meta = BioCypherEdge(
                self.db_meta.graph_state["id"],
                self.db_meta.node_id,
                "PRECEDES",
            )
            self.add_biocypher_edges(e_meta)

        # add structure nodes
        no_l = []
        # leaves of the hierarchy specified in schema yaml
        for entity, params in self.db_meta.leaves.items():
            no_l.append(MetaNode(entity, **params))
        self.add_biocypher_nodes(no_l)

        # remove connection of structure nodes from previous version
        # node(s)
        self.query("MATCH ()-[r:CONTAINS]-()" "DELETE r")

        # connect structure nodes to version node
        ed_v = []
        current_version = self.db_meta.get_id()
        for entity in self.db_meta.leaves.keys():
            ed_v.append(MetaEdge(current_version, entity, "CONTAINS"))
        self.add_biocypher_edges(ed_v)

        # add graph structure between MetaNodes
        ed = []
        for no in no_l:
            id = no.get_id()
            src = no.get_properties().get("source")
            tar = no.get_properties().get("target")
            if not None in [id, src, tar]:
                ed.append(BioCypherEdge(id, src, "IS_SOURCE_OF"))
                ed.append(BioCypherEdge(id, tar, "IS_TARGET_OF"))
        self.add_biocypher_edges(ed)

    def init_db(self):
        """
        Used to initialise a property graph database by deleting
        contents and constraints and setting up new constraints.

        Todo:
            - set up constraint creation interactively depending on the
                need of the database
        """

        self.wipe_db()
        self._create_constraints()
        logger.info("Initialising database.")

    def _create_constraints(self):
        """
        Creates constraints on node types in the graph. Used for
        initial setup.

        Grabs leaves of the ``schema_config.yaml`` file and creates
        constraints on the id of all entities represented as nodes.
        """

        logger.info(f"Creating constraints for node types in config.")

        # get structure
        for leaf in self.db_meta.leaves.items():
            label = leaf[0]
            if leaf[1]["represented_as"] == "node":

                s = (
                    f"CREATE CONSTRAINT {label}_id "
                    f"IF NOT EXISTS ON (n:{label}) "
                    "ASSERT n.id IS UNIQUE"
                )
                self.query(s)

    def add_nodes(self, id_type_tuples):
        """
        Generic node adder method to add any kind of input to the
        graph via the :class:`biocypher.create.BioCypherNode` class. Employs translation
        functionality and calls the :meth:`add_biocypher_nodes()` method.

        Args:
            id_type_tuples (iterable of 3-tuple): for each node to add to
                the biocypher graph, a 3-tuple with the following layout:
                first, the (unique if constrained) ID of the node; second, the
                type of the node, capitalised or PascalCase and in noun form
                (Neo4j primary label, eg `:Protein`); and third, a dictionary
                of arbitrary properties the node should possess (can be empty).

        Returns:
            2-tuple: the query result of :meth:`add_biocypher_nodes()`
                - first entry: data
                - second entry: Neo4j summary.
        """

        bn = gen_translate_nodes(self.db_meta.schema, id_type_tuples)
        return self.add_biocypher_nodes(bn)

    def add_edges(self, src_tar_type_tuples):
        """
        Generic edge adder method to add any kind of input to the
        graph via the :class:`biocypher.create.BioCypherEdge` class.
        Employs translation functionality and calls the
        :meth:`add_biocypher_edges()` method.

        Args:
            id_type_tuples (iterable of 4-tuple): for each edge to add to
                the biocypher graph, a 4-tuple with the following layout:
                first and second, the (unique if constrained) IDs of the
                source and target nodes of the relationship; third, the
                type of the relationship, all caps with underscores and
                in verb form (Neo4j primary label, eg `:IS_TARGET_OF`);
                and fourth, a dictionary of arbitrary properties the edge
                should possess (can be empty).

        Returns:
            2-tuple: the query result of :meth:`add_biocypher_edges()`
                - first entry: data
                - second entry: Neo4j summary.
        """

        bn = gen_translate_edges(self.db_meta.schema, src_tar_type_tuples)
        return self.add_biocypher_edges(bn)

    def add_biocypher_nodes(self, nodes, explain=False, profile=False):
        """
        Accepts a node type handoff class
        (:class:`biocypher.create.BioCypherNode`) with id,
        label, and a dict of properties (passing on the type of
        property, ie, ``int``, ``str``, ...).

        The dict retrieved by the
        :meth:`biocypher.create.BioCypherNode.get_dict()` method is
        passed into Neo4j as a map of maps, explicitly encoding node id
        and label, and adding all other properties from the 'properties'
        key of the dict. The merge is performed via APOC, matching only
        on node id to prevent duplicates. The same properties are set on
        match and on create, irrespective of the actual event.

        Args:
            nodes (iterable of BioCypherNode): a list of
                :class:`biocypher.create.BioCypherNode` objects
            explain (bool): whether to call ``EXPLAIN`` in front of the
                CYPHER query
            profile (bool): whether to call ``PROFILE`` in front of the
                CYPHER query

        Returns:
            bool: The return value. True for success, False otherwise.

        Todo:
            - use return nodes to implement test?
        """

        # receive generator objects
        if isinstance(nodes, GeneratorType):
            nodes = peekable(nodes)
            if not isinstance(nodes.peek(), BioCypherNode):
                logger.warn(
                    "It appears that the first node is not a BioCypherNode. "
                    "Nodes must be passed as type BioCypherNode. "
                    "Please use the generic add_edges() method."
                )
                return (False, False)
            else:
                logger.info("Merging nodes from generator.")

        # receive single nodes or node lists
        else:
            if type(nodes) is not list:
                nodes = [nodes]
            if not all(isinstance(n, BioCypherNode) for n in nodes):
                logger.error("Nodes must be passed as type BioCypherNode.")
                return (False, False)
            else:
                logger.info(f"Merging {len(nodes)} nodes.")

        entities = [node.get_dict() for node in nodes]

        entity_query = (
            "UNWIND $entities AS ent "
            "CALL apoc.merge.node([ent.node_label], "
            "{id: ent.node_id}, ent.properties, ent.properties) "
            "YIELD node "
            "RETURN node"
        )

        if explain:
            return self.explain(
                entity_query, parameters={"entities": entities}
            )
        elif profile:
            return self.profile(
                entity_query, parameters={"entities": entities}
            )
        else:
            res = self.query(entity_query, parameters={"entities": entities})
            logger.info("Finished merging nodes.")
            return res

    def add_biocypher_edges(self, edges, explain=False, profile=False):
        """
        Accepts an edge type handoff class
        (:class:`biocypher.create.BioCypherEdge`) with source
        and target ids, label, and a dict of properties (passing on the
        type of property, ie, int, string ...).

        The individual edge is either passed as a singleton, in the case
        of representation as an edge in the graph, or as a 4-tuple, in
        the case of representation as a node (with two edges connecting
        to interaction partners).

        The dict retrieved by the
        :meth:`biocypher.create.BioCypherEdge.get_dict()` method is
        passed into Neo4j as a map of maps, explicitly encoding source
        and target ids and the relationship label, and adding all edge
        properties from the 'properties' key of the dict. The merge is
        performed via APOC, matching only on source and target id to
        prevent duplicates. The same properties are set on match and on
        create, irrespective of the actual event.

        Args:
            edges: a list of :class:`biocypher.create.BioCypherEdge` objects
            explain (bool): whether to call ``EXPLAIN`` in front of the
                CYPHER query
            profile (bool): whether to call ``PROFILE`` in front of the
                CYPHER query

        Returns:
            bool: The return value. True for success, False otherwise.
        """

        rel_as_node = False

        # receive generator objects
        if isinstance(edges, GeneratorType):
            # itertools solution is kind of slow and cumbersome
            # however, needs to detect tuples...

            edges = peekable(edges)

            if isinstance(edges.peek(), BioCypherRelAsNode):
                # create one node and two edges
                rel_as_node = True
                logger.info("Merging nodes and edges from generator.")

        # receive single edges or edge lists
        else:
            if type(edges) is not list:
                edges = [edges]

            # flatten
            if any(isinstance(i, list) for i in edges):
                edges = [item for sublist in edges for item in sublist]

            if isinstance(edges[0], BioCypherRelAsNode):
                rel_as_node = True
            elif not all(isinstance(e, BioCypherEdge) for e in edges):
                logger.error("Nodes must be passed as type BioCypherEdge.")
                return (False, False)

            logger.info("Merging %s edges." % len(edges))

        if rel_as_node:
            # split up tuples in nodes and edges if detected
            z = zip(
                *(
                    (
                        e.get_node(),
                        [e.get_source_edge(), e.get_target_edge()],
                    )
                    for e in edges
                )
            )
            nod, edg = [list(a) for a in z]
            self.add_biocypher_nodes(nod)
            self.add_biocypher_edges(edg)

        # cypher query
        else:
            rels = [edge.get_dict() for edge in edges]

            # merging only on the ids of the entities, passing the
            # properties on match and on create;
            # TODO add node labels?
            node_query = (
                "UNWIND $rels AS r "
                "MERGE (src {id: r.source_id}) "
                "MERGE (tar {id: r.target_id}) "
            )
            self.query(node_query, parameters={"rels": rels})

            edge_query = (
                "UNWIND $rels AS r "
                "MATCH (src {id: r.source_id}) "
                "MATCH (tar {id: r.target_id}) "
                "WITH src, tar, r "
                "CALL apoc.merge.relationship"
                "(src, r.relationship_label, NULL, "
                "r.properties, tar, r.properties) "
                "YIELD rel "
                "RETURN rel"
            )

            if explain:
                return self.explain(edge_query, parameters={"rels": rels})
            elif profile:
                return self.profile(edge_query, parameters={"rels": rels})
            else:
                res = self.query(edge_query, parameters={"rels": rels})
                logger.info("Finished merging edges.")
                return res

    def write_nodes(self, nodes, dirname=None, db_name=None):
        """
        Write BioCypher nodes to disk using the :mod:`write` module,
        formatting the CSV to enable Neo4j admin import from the target
        directory.

        Args:
            nodes (iterable): collection of nodes to be written in
                BioCypher-compatible CSV format; can be any compatible
                (ie, translatable) input format or already as
                :class:`biocypher.create.BioCypherNode`.
        """

        # instantiate adapter on demand because it takes time to load
        # the biolink model toolkit
        if not self.bl_adapter:
            self.bl_adapter = BiolinkAdapter(self.db_meta.leaves)

        if not self.batch_writer:
            self.batch_writer = BatchWriter(
                self.db_meta.schema,
                self.bl_adapter,
                dirname=dirname,
                db_name=db_name or self._db_name,
            )

        nodes = peekable(nodes)
        if not isinstance(nodes.peek(), BioCypherNode):
            tnodes = gen_translate_nodes(self.db_meta.leaves, nodes)
        else:
            tnodes = nodes
        # write node files
        self.batch_writer.write_nodes(tnodes)

    def write_edges(self, edges, dirname=None, db_name=None):
        """
        Write BioCypher edges to disk using the :mod:`write` module,
        formatting the CSV to enable Neo4j admin import from the target
        directory.

        Args:
            edges (iterable): collection of edges to be written in
                BioCypher-compatible CSV format; can be any compatible
                (ie, translatable) input format or already as
                :class:`biocypher.create.BioCypherEdge`.
        """

        # instantiate adapter on demand because it takes time to load
        # the biolink model toolkit
        if not self.bl_adapter:
            self.bl_adapter = BiolinkAdapter(self.db_meta.leaves)

        if not self.batch_writer:
            self.batch_writer = BatchWriter(
                self.db_meta.schema,
                self.bl_adapter,
                dirname=dirname,
                db_name=db_name or self._db_name,
            )

        edges = peekable(edges)
        if not isinstance(edges.peek(), BioCypherEdge):
            tedges = gen_translate_edges(self.db_meta.leaves, edges)
        else:
            tedges = edges
        # write edge files
        self.batch_writer.write_edges(tedges)

    def get_import_call(self):
        """
        Upon using the batch writer for writing admin import CSV files,
        return a string containing the neo4j admin import call with
        delimiters, database name, and paths of node and edge files.

        Returns:
            str: a neo4j-admin import call
        """
        return self.batch_writer.get_import_call()

    def write_import_call(self):
        """
        Upon using the batch writer for writing admin import CSV files,
        write a string containing the neo4j admin import call with
        delimiters, database name, and paths of node and edge files, to
        the export directory.

        Returns:
            bool: The return value. True for success, False otherwise.
        """
        return self.batch_writer.write_import_call()
