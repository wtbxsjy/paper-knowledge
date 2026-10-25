"""Journal abbreviations and directory naming. Pure rules.

``JOURNAL_ABBREV_MAP`` is hand-maintained on purpose: Sciverse venue names are
full names and agreed with CrossRef abbreviations in only 12/35 papers, so it
cannot replace this table.
"""
from __future__ import annotations

import re

JOURNAL_ABBREV_MAP = {
    'nature': 'Nature',
    'nature medicine': 'NatMed',
    'nature reviews bioengineering': 'NatRevBioeng',
    'nature biomedical engineering': 'NatBiomedEng',
    'nature communications': 'NatCommun',
    'nature protocols': 'NatProtocols',
    'nature reviews microbiology': 'NatRevMicrobiol',
    'nature genetics': 'NatGenet',
    'nature cancer': 'NatCan',
    'nature aging': 'NatAging',
    'cell': 'Cell',
    'cell reports': 'CellRep',
    'cell biomaterials': 'CellBiomat',
    'cancer cell': 'CancerCell',
    'neuron': 'Neuron',
    'proceedings of the national academy of sciences': 'PNAS',
    'bmc': 'BMC',
    'bioinformatics': 'Bioinformatics',
    'chemical science': 'Chem',
    'journal of chemical information and modeling': 'JChemInfModel',
    'accounts of chemical research': 'AccChemRes',
    'pharmaceuticals': 'Pharmaceuticals',
    'pharmacological reviews': 'PharmacolRev',
    'signal transduction and targeted therapy': 'STTT',
    'molecular biology reports': 'MolBiolRep',
    'international journal of peptide research and therapeutics': 'IntJPeptResTher',
    'trends in food science & technology': 'TrendsFoodSciTech',
    'science translational medicine': 'SciTranslMed',
    'science advances': 'SciAdv',
    'molecular cancer': 'MolCancer',
    'brain : a journal of neurology': 'Brain',
    'medrxiv : the preprint server for health': 'medRxiv',
    'biorxiv': 'BioRxiv',
    'science': 'Science',
    'pnas': 'PNAS',
    'chem': 'Chem',
    'mdpi': 'MDPI',
}


def abbreviate_journal(journal_name: str) -> str:
    """Map full journal name to short abbreviation."""
    name_lower = journal_name.lower().strip().rstrip(".")
    if name_lower in JOURNAL_ABBREV_MAP:
        return JOURNAL_ABBREV_MAP[name_lower]
    # Generate abbreviation: take first letter of each word (PascalCase)
    words = re.findall(r"[A-Za-z]{2,}", name_lower)
    abbr = "".join(w[0].upper() + w[1:3] for w in words[:4])
    return abbr[:20] or "Journal"


def generate_short_title(title: str) -> str:
    """Generate a short PascalCase slug from a title."""
    words = re.findall(r"[A-Za-z0-9]{2,}", title)
    stopwords = {"the", "and", "for", "of", "in", "with", "from", "that", "this",
                 "are", "was", "has", "been", "its", "not", "but", "via"}
    keywords = [w.capitalize() for w in words if w.lower() not in stopwords]
    return "_".join(keywords[:5])[:50]


def build_clean_name_from_meta(first_author: str, year: int, journal: str, title: str) -> str:
    """Build clean directory name: {FirstAuthor}{Year}_{JournalAbbrev}_{ShortTitle}."""
    first_author = re.sub(r"[^A-Za-z]", "", first_author)
    journal_abbrev = abbreviate_journal(journal)
    short_title = generate_short_title(title)
    name = f"{first_author}{year}_{journal_abbrev}_{short_title}"
    name = re.sub(r'[<>:"/\\|?*]', "", name)
    name = re.sub(r"\s+", "_", name)
    return name[:120]
