# import json
import sys
# from rdflib import Graph, URIRef, RDF, OWL, RDFS, Literal, Namespace, DC, BNode
from hashlib import md5

from rdflib import Graph, RDF, OWL, RDFS, Literal, BNode, URIRef
from rdflib.term import Identifier

from omim2obo.namespaces import *
from omim2obo.parsers.omim_entry_parser import cleanup_label, get_alt_labels, get_pubs, get_mapped_ids
# from omim2obo.omim_client import OmimClient
from omim2obo.config import config, DATA_DIR, ROOT_DIR
from omim2obo.parsers.omim_txt_parser import *
# from omim2obo.omim_code_scraper.omim_code_scraper import get_codes_by_yyyy_mm


LOG = logging.getLogger(__name__)
LOG.setLevel(logging.DEBUG)
LOG.addHandler(logging.StreamHandler(sys.stdout))

DOWNLOAD_TXT_FILES = False


def get_curie_maps():
    map_file = DATA_DIR / 'dipper/curie_map.yaml'
    with open(map_file, "r") as f:
        maps = yaml.safe_load(f)
    return maps


CURIE_MAP = get_curie_maps()


class DeterministicBNode(BNode):
    """Overrides BNode to create a deterministic ID"""

    def __new__(cls, source_ref: str):
        """Constructor
            source_ref: A reference to be passed to MD5 to generate id.
        """
        id: str = md5(source_ref.encode()).hexdigest()
        return Identifier.__new__(cls, id)


class OmimGraph(Graph):
    __instance: Graph = None

    @staticmethod
    def get_graph():
        if OmimGraph.__instance is None:
            OmimGraph.__instance = Graph()
            for ns_prefix, ns_uri in CURIE_MAP.items():
                OmimGraph.__instance.namespace_manager.bind(ns_prefix, URIRef(ns_uri))
        return OmimGraph.__instance


def run():
    """Run program"""
    graph = OmimGraph.get_graph()
    for prefix, uri in CURIE_MAP.items():
        graph.namespace_manager.bind(prefix, URIRef(uri))

    # Parse mimTitles.txt
    omim_titles, omim_replaced = parse_mim_titles(retrieve_mim_file('mimTitles.txt', DOWNLOAD_TXT_FILES))
    omim_ids = list(omim_titles.keys() - omim_replaced.keys())

    LOG.info('Have %i omim numbers from mimTitles.txt', len(omim_ids))
    LOG.info('Have %i total omim types ', len(omim_titles))

    tax_label = 'Homo sapiens'
    tax_id = GLOBAL_TERMS[tax_label]

    tax_uri = URIRef(tax_id)
    graph.add((tax_uri, RDF.type, OWL.Class))
    graph.add((tax_uri, RDFS.label, Literal(tax_label)))

    for omim_id in omim_ids:
        omim_uri = OMIM[omim_id]
        graph.add((omim_uri, RDF.type, OWL.Class))
        omim_type, pref_label, alt_label, inc_label = omim_titles[omim_id]

        label = pref_label
        other_labels = []
        if alt_label:
            other_labels += get_alt_labels(alt_label)
        if inc_label:
            other_labels += get_alt_labels(inc_label)

        # Labels
        abbrev = label.split(';')[1].strip() if ';' in label else None

        if omim_type == OmimType.HERITABLE_PHENOTYPIC_MARKER:  # %
            graph.add((omim_uri, RDFS.label, Literal(cleanup_label(label))))
            graph.add((omim_uri, BIOLINK['category'], BIOLINK['Disease']))
        elif omim_type == OmimType.GENE or omim_type == OmimType.HAS_AFFECTED_FEATURE:  # * or +
            omim_type = OmimType.GENE
            graph.add((omim_uri, RDFS.label, Literal(abbrev)))
            graph.add((omim_uri, RDFS.subClassOf, SO['0000704']))
            graph.add((omim_uri, BIOLINK['category'], BIOLINK['Gene']))
        elif omim_type == OmimType.PHENOTYPE:  # #
            graph.add((omim_uri, RDFS.label, Literal(cleanup_label(label))))
            graph.add((omim_uri, BIOLINK['category'], BIOLINK['Disease']))
        else:
            graph.add((omim_uri, RDFS.label, Literal(cleanup_label(label))))

        exact_labels = [s.strip() for s in label.split(';')]
        if len(exact_labels) > 1:  # the last string is an abbreviation. Add OWL reification. See issue #2
            abbr = exact_labels.pop()
            graph.add((omim_uri, oboInOwl.hasExactSynonym, Literal(abbr)))
            axiom_id = DeterministicBNode(abbr)
            graph.add((axiom_id, RDF.type, OWL.Axiom))
            graph.add((axiom_id, OWL.annotatedSource, CL['0017543']))
            graph.add((axiom_id, OWL.annotatedProperty, oboInOwl.hasExactSynonym))
            graph.add((axiom_id, OWL.annotatedTarget, Literal(abbr)))
            graph.add((axiom_id, oboInOwl.hasSynonymType, MONDONS.ABBREVIATION))

        for exact_label in exact_labels:
            graph.add((omim_uri, oboInOwl.hasExactSynonym, Literal(cleanup_label(exact_label, abbrev))))

        for label in other_labels:
            graph.add((omim_uri, oboInOwl.hasRelatedSynonym, Literal(cleanup_label(label, abbrev))))

    # Gene ID
    gene_map, pheno_map = parse_mim2gene(retrieve_mim_file('mim2gene.txt', DOWNLOAD_TXT_FILES))
    for mim_number, entrez_id in gene_map.items():
        graph.add((OMIM[mim_number], OWL.equivalentClass, NCBIGENE[entrez_id]))
    for mim_number, entrez_id in pheno_map.items():
        graph.add((NCBIGENE[entrez_id], RO['0002200'], OMIM[mim_number]))

    # Phenotpyic Series
    pheno_series = parse_phenotypic_series_titles(retrieve_mim_file('phenotypicSeries.txt', DOWNLOAD_TXT_FILES))
    for ps in pheno_series:
        graph.add((OMIMPS[ps], RDF.type, OWL.Class))
        graph.add((OMIMPS[ps], RDFS.label, Literal(pheno_series[ps][0])))
        graph.add((OMIMPS[ps], BIOLINK.category, BIOLINK.Disease))
        for mim_number in pheno_series[ps][1]:
            graph.add((OMIM[mim_number], RDFS.subClassOf, OMIMPS[ps]))

    # Morbid map (cyto locations)
    morbid_map = parse_morbid_map(retrieve_mim_file('morbidmap.txt', DOWNLOAD_TXT_FILES))
    for mim_number in morbid_map:
        phenotype_mim_number, cyto_location = morbid_map[mim_number]
        if phenotype_mim_number:
            graph.add((OMIM[mim_number], RO['0003303'], OMIM[phenotype_mim_number]))
        if cyto_location:
            chr_id = '9606chr' + cyto_location
            graph.add((OMIM[mim_number], RO['0002525'], CHR[chr_id]))

    # PUBMED, UMLS
    pmid_map, umls_map, orphanet_map = get_maps_from_turtle()

    # Get the recent updated
    updated_entries = get_updated_entries()
    for entry in updated_entries:
        entry = entry['entry']
        mim_number = str(entry['mimNumber'])
        pmid_map[mim_number] = get_pubs(entry)
        external_maps = get_mapped_ids(entry)
        umls_map[mim_number] = external_maps[UMLS]
        orphanet_map[mim_number] = external_maps[ORPHANET]

    for mim_number, pm_ids in pmid_map.items():
        for pm_id in pm_ids:
            graph.add((OMIM[mim_number], IAO['0000142'], PMID[pm_id]))
    for mim_number, umlsids in umls_map.items():
        for umls_id in umlsids:
            graph.add((OMIM[mim_number], oboInOwl.hasDbXref, UMLS[umls_id]))
    for mim_number, orphanet_ids in orphanet_map.items():
        for orphanet_id in orphanet_ids:
            graph.add((OMIM[mim_number], oboInOwl.hasDbXref, ORPHANET[orphanet_id]))

    # replaced
    for mim_num, replaced in omim_replaced.items():
        if len(replaced) > 1:
            for replaced_mim_num in replaced:
                graph.add((OMIM[mim_num], oboInOwl.consider, OMIM[replaced_mim_num]))
        elif replaced:
            graph.add((OMIM[mim_num], IAO['0100001'], OMIM[replaced[0]]))
        graph.add((OMIM[mim_num], RDF.type, OWL.Class))
        graph.add((OMIM[mim_num], OWL.deprecated, Literal(True)))

    # with open(ROOT_DIR / 'omim.ttl', 'w') as f:
    #     f.write(graph.serialize(format='turtle'))
    with open(ROOT_DIR / 'omim.xml', 'w') as f:
        f.write(graph.serialize(format='xml'))
    print("Job's done ;3")


if __name__ == '__main__':
    run()
