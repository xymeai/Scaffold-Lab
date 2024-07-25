import os
import numpy as np
import subprocess
import typing as T
import random
import shutil
import json
import logging
import pandas as pd
from typing import *
from pathlib import Path

import mdtraj as md
import MDAnalysis as mda
import biotite
import biotite.structure.io as strucio
import biotite.structure as struc
import biotite.structure.io.pdb as pdb
import biotite.application.dssp as dssp
from biotite.structure.residues import get_residues
from biotite.structure import get_chains
from biotite.sequence import ProteinSequence
from biotite.sequence.io import fasta
from tmtools import tm_align

log = logging.getLogger(__name__)

def motif_extract(
    position: str,
    structure_path: Union[str, struc.AtomArray],
    atom_part: Optional[str] = "all-atom",
    split_char: str = "/"
) -> struc.AtomArray:
    """Extracting motif positions from input protein structure.

    Args:
        position (str): Motif region of input protein. DEMO: "A1-7/A28-79" corresponds defines res1-7 and res28-79 in chain A to be motif. 
        structure_path (Union[str, None]): Input protein structure, can either be a path or an AtomArray.
        atom_part (str, optional): _description_. Defaults to "all".
        split_char (str): Spliting character between discontinuous motifs. Defaults to "/".

    Returns:
        motif (biotite.structure.AtomArray): The motif positions extracted by user-specified way (all-atom / CA / backbone)
    """
    
    position = position.split(split_char)
    if isinstance(structure_path, str):
        array = strucio.load_structure(structure_path, model=1)
    else:
        array = structure_path
        
    motif_array = []
    for i in position:
        chain_id = i[0]
        i = i.replace(chain_id, "")
        if "-" not in i: # Single-residue motif
            start = end = int(i)
        else:
            start, end = i.split("-")
            #print(start, end)
            log.info(f'Motif position from {start} to {end}')
            start, end = int(start), int(end)
            
        if atom_part == "all-atom":
            motif_array.append(array[(array.chain_id==chain_id) & (array.res_id <= end) & (array.res_id >= start) & (array.hetero==False)])
        elif atom_part == "CA":
            motif_array.append(array[(array.chain_id==chain_id) & (array.res_id <= end) & (array.res_id >= start) & (array.hetero==False) & (array.atom_name=="CA")])
        elif atom_part == "backbone":
            motif_array.append(array[(array.chain_id==chain_id) & (array.res_id <= end) & (array.res_id >= start) & (array.hetero==False) & ((array.atom_name=="N") | (array.atom_name=="CA")| (array.atom_name=="C") | (array.atom_name=="O"))])
            
    motif = motif_array[0]
    for i in range(len(motif_array) - 1):
        motif += motif_array[i + 1]
    return motif

def rmsd(
    reference: Union[str, struc.AtomArray],
    target: Union[str, struc.AtomArray],
    
) -> float:
    
    # Handle input protein type
    if isinstance(reference, str):
        reference = strucio.load_structure(reference, model=1)
    if isinstance(target, str):
        target = strucio.load_structure(target, model=1)
        
    mask_reference = (((reference.atom_name == "N") | (reference.atom_name == "CA") | (reference.atom_name == "C") | (reference.atom_name == "O")) & (biotite.structure.filter_amino_acids(reference)))
    reference = reference[mask_reference]
    mask_target = (((target.atom_name == "N") | (target.atom_name == "CA") | (target.atom_name == "C") | (target.atom_name == "O")) & (biotite.structure.filter_amino_acids(target)) )
    target = target[mask_target]
    superimposed, _ = struc.superimpose(reference, target)
    rms = struc.rmsd(reference, superimposed)
    return rms

def calculate_secondary_structure(
    input: Union[str, struc.AtomArray] = None,
) -> Optional[list]:
    """
    Calculate protein secondary structure.
    """
    array = strucio.load_structure(input)
    app = dssp.DsspApp(array)
    app.start()
    app.join()
    sse = app.get_sse()
    sse_format = "".join(sse)
    
    alpha_composition = (sse_format.count("H") + sse_format.count("G") + sse_format.count("I")) / len(sse_format) if len(sse_format) > 0 else 0
    beta_composition = (sse_format.count("B") + sse_format.count("E")) / len(sse_format) if len(sse_format) > 0 else 0
    coil_composition = (sse_format.count("C") + sse_format.count("S")) / len(sse_format) if len(sse_format) > 0 else 0
    turn_composition = sse_format.count("T") / len(sse_format) if len(sse_format) > 0 else 0

    return [sse_format, alpha_composition, beta_composition, coil_composition, turn_composition]

def radius_of_gyration(
    input: str,
    atom_part: Optional[str] = "all-atom",
    ) -> float:
    t = mda.Universe(input)
    if atom_part == 'all-atom':
        rg = t.atoms.radius_of_gyration()
    if atom_part == 'backbone':
        backbone = t.select_atoms("backbone") 
        rg = backbone.atoms.radius_of_gyration()
    if atom_part == 'CA':
        c_alpha = t.select_atoms("name CA")
        rg = c_alpha.atoms.radius_of_gyration()
    return round(rg, 3)


def calc_tm_score(pos_1, pos_2, seq_1, seq_2):
    tm_results = tm_align(pos_1, pos_2, seq_1, seq_2)
    return tm_results.tm_norm_chain1, tm_results.tm_norm_chain2

def calc_aligned_rmsd(pos_1, pos_2):
    aligned_pos_1 = rigid_transform_3D(pos_1, pos_2)[0]
    return np.mean(np.linalg.norm(aligned_pos_1 - pos_2, axis=-1))

def rigid_transform_3D(A, B, verbose=False):
    # Transforms A to look like B
    # https://github.com/nghiaho12/rigid_transform_3D
    assert A.shape == B.shape
    A = A.T
    B = B.T

    num_rows, num_cols = A.shape
    if num_rows != 3:
        raise Exception(f"matrix A is not 3xN, it is {num_rows}x{num_cols}")

    num_rows, num_cols = B.shape
    if num_rows != 3:
        raise Exception(f"matrix B is not 3xN, it is {num_rows}x{num_cols}")

    # find mean column wise
    centroid_A = np.mean(A, axis=1)
    centroid_B = np.mean(B, axis=1)

    # ensure centroids are 3x1
    centroid_A = centroid_A.reshape(-1, 1)
    centroid_B = centroid_B.reshape(-1, 1)

    # subtract mean
    Am = A - centroid_A
    Bm = B - centroid_B

    H = Am @ np.transpose(Bm)

    # sanity check
    #if linalg.matrix_rank(H) < 3:
    #    raise ValueError("rank of H = {}, expecting 3".format(linalg.matrix_rank(H)))

    # find rotation
    U, S, Vt = np.linalg.svd(H)
    R = Vt.T @ U.T

    # special reflection case
    reflection_detected = False
    if np.linalg.det(R) < 0:
        if verbose:
            print("det(R) < R, reflection detected!, correcting for it ...")
        Vt[2,:] *= -1
        R = Vt.T @ U.T
        reflection_detected = True

    t = -R @ centroid_A + centroid_B
    optimal_A = R @ A + t

    return optimal_A.T, R, t, reflection_detected

def parse_pdb_feats(
        pdb_name: str,
        pdb_path: str,
        scale_factor=1.,
        chain_id='A',
    ):
    """
    Only used in inference procedure to prepare ESMFold prediction.
    
    Args:
        pdb_name: name of PDB to parse.
        pdb_path: path to PDB file to read.
        scale_factor: factor to scale atom positions.
        mean_center: whether to mean center atom positions.
    Returns:
        Dict with CHAIN_FEATS features extracted from PDB with specified
        preprocessing.
    """
    parser = PDB.PDBParser(QUIET=True)
    structure = parser.get_structure(pdb_name, pdb_path)
    struct_chains = {
        chain.id: chain
        for chain in structure.get_chains()}

    def _process_chain_id(x):
        chain_prot = process_chain(struct_chains[x], x)
        chain_dict = dataclasses.asdict(chain_prot)

        # Process features
        feat_dict = {x: chain_dict[x] for x in CHAIN_FEATS}
        return parse_chain_feats(
            feat_dict, scale_factor=scale_factor)

    if isinstance(chain_id, str):
        return _process_chain_id(chain_id)
    elif isinstance(chain_id, list):
        return {
            x: _process_chain_id(x) for x in chain_id
        }
    elif chain_id is None:
        return {
            x: _process_chain_id(x) for x in struct_chains
        }
    else:
        raise ValueError(f'Unrecognized chain list {chain_id}')

def randomly_select_and_copy_pdb_files(src_folder, dst_folder, num_files):
    """
    Randomly select and copy a specified number of PDB files 
    from a source folder to a destination folder.

    :param src_folder: Path to the source folder containing PDB files.
    :param dst_folder: Path to the destination folder where files will be copied.
    :param num_files: Number of PDB files to randomly select and copy.
    """
    # Get a list of all PDB files in the source folder
    pdb_files = [file for file in os.listdir(src_folder) if file.endswith('.pdb')]

    # Check if the source folder has enough files
    if len(pdb_files) < num_files:
        raise ValueError(f"Source folder has only {len(pdb_files)} files, but {num_files} files are requested.")

    # Randomly select the specified number of PDB files
    selected_files = random.sample(pdb_files, num_files)

    # Create the destination folder if it doesn't exist
    if not os.path.exists(dst_folder):
        os.makedirs(dst_folder)

    # Copy each selected file to the destination folder
    for file in selected_files:
        shutil.copy(os.path.join(src_folder, file), os.path.join(dst_folder, file))

def cleanup_af2_outputs(
    af2_dir: Union[str, Path],
    path_to_store: Union[str, Path],
    remove_after_cleanup: Optional = False
) -> Dict:

    output_dict = {}
    for file in os.listdir(af2_dir):
        if file.endswith('.pdb'):
            if file.startswith('T_0') == False: # original backbone sequence
                new_path = os.path.join(path_to_store, 'sample_0.pdb')
                shutil.copy2(os.path.join(af2_dir, file), new_path)
                output_dict['sample_0'] = {}
                output_dict['sample_0']['sample_path'] = os.path.abspath(new_path)
            else: # Designed sequence
                sample_index = os.path.splitext(file)[0].split('sample_')[1].split('__score')[0]
                new_path = os.path.join(path_to_store, f'sample_{sample_index}.pdb')
                shutil.copy2(os.path.join(af2_dir, file), new_path)
                if f'sample_{sample_index}' not in output_dict:
                    output_dict[f'sample_{sample_index}'] = {}
                output_dict[f'sample_{sample_index}']['sample_path'] = os.path.abspath(new_path)
        elif file.endswith('.json') and 'rank' in file: # file storing pLDDT & pTM & pAE
            if file.startswith('T_0'):
                sample_index = os.path.splitext(file)[0].split('sample_')[1].split('__score')[0]
                if f'sample_{sample_index}' not in output_dict:
                    output_dict[f'sample_{sample_index}'] = {}
                with open(os.path.join(af2_dir, file), 'r') as f:
                    j = json.load(f)
                    output_dict[f'sample_{sample_index}']['plddt'] = np.mean(j['plddt'])
                    output_dict[f'sample_{sample_index}']['pae'] = np.mean(j['pae'])
                    output_dict[f'sample_{sample_index}']['ptm'] = np.mean(j['ptm'])
            else: # original backbone sequence
                if 'sample_0' not in output_dict:
                    output_dict['sample_0'] = {}
                with open(os.path.join(af2_dir, file), 'r') as f:
                    j = json.load(f)
                    output_dict['sample_0']['plddt'] = np.mean(j['plddt'])
                    output_dict['sample_0']['pae'] = np.mean(j['pae'])
                    output_dict['sample_0']['ptm'] = np.mean(j['ptm'])
    return output_dict

def write_seqs_to_fasta(
    input_seqs: Union[list],
    fasta_path: Union[str, Path]
) -> None:
    fasta_instance = fasta.FastaFile()
    for i, (mpnn_score, header, string) in enumerate(input_seqs):
        fasta_instance[header] = string
    fasta_instance.write(fasta_path)

def get_csv_data(
    csv_info: Union[str, Path],
    pdb_name: str,
    sample_num: Union[str, int],
    save_csv: Optional[Union[str, Path]] = None
) -> Tuple:
    """Index information from input csv file.

    Args:
        csv_info (Union[str, Path]): CSV file containing motif information. (Template: ./demo/motif_scaffolding/motif_info.csv)
        pdb_name (str): The name of sampled PDB file with format {pdb_name}_{sample_num}.pdb. e.g. 2KL8_33.pdb -> 2KL8
        sample_num (Union[str, int]): Number of samples with format {pdb_name}_{sample_num}.pdb. e.g. 2KL8_33.pdb -> 33

    Returns:
        Tuple: A tuple containing information from each column. 
            contig (str): Motif and scaffold information, where motifs start with characters and scaffolds start with numbers.
            mask (str): 1D boolean array containing motif positions. True -> motif, False -> scaffold
            motif_indices (str): List containing motif positions
            redesign_positions (str): Positions to be redesigned, segmented by ';'
    """
    csv_info = pd.read_csv(csv_info)
    csv_info['sample_num'] = csv_info['sample_num'].astype(int)
    sample_item = csv_info[(csv_info['pdb_name'] == pdb_name) & (csv_info['sample_num'] == int(sample_num))]
    if not sample_item.empty:
        contig = sample_item['contig'].iloc[0]
        length, motif_indices, motif_mask = generate_indices_and_mask(contig)
        if 'motif_indices' not in csv_info.columns:
            csv_info['motif_indices'] = None
        if 'mask' not in csv_info.columns:
            csv_info['mask'] = None

        csv_info.at[sample_item.index[0], 'motif_indices'] = motif_indices
        csv_info.at[sample_item.index[0], 'mask'] = motif_mask

        return(
            sample_item['contig'].iloc[0],
            motif_mask,
            motif_indices,
            sample_item['redesign_positions'].iloc[0] if 'redesign_positions' in sample_item.columns and not pd.isna(sample_item['redesign_positions'].iloc[0]) else None
        )


def motif_indices_to_contig(motif_indices: Union[List, str]) -> str:
    """Extract motif contig from overall contig. 
    e.g. "A1-7/20-20/A28-79" -> "A1-7/A28-79"
    TBD: Support multiple chains beyond chain A.
    
    Args:
        motif_indices (str): The str object of motif list from "motif_indices" returned by `get_csv_data()`. 

    Returns:
        contig: Contig containing motif information.
    """
    if isinstance(motif_indices, list):
        motif_indices = motif_indices
    elif isinstance(motif_indices, str) and motif_indices.startswith('[') and motif_indices.endswith(']'):
        motif_indices = motif_indices.strip('[]').split(', ')
        try:
            motif_indices = [int(index) for index in motif_indices]
        except ValueError as e:
            raise ValueError(f"Error converting motif_indices_str to list of integers: {e}")
    else:
        raise ValueError(f"Invalid input for motif_indices_to_contig: {motif_indices}")

    sorted_indices = sorted(motif_indices)
    contig = ""
    range_start = None

    for i, index in enumerate(sorted_indices):
        if range_start is None:
            range_start = index
        if i == len(sorted_indices) - 1 or sorted_indices[i + 1] != index + 1:
            if contig:
                contig += "/"
            if range_start == index:
                contig += f"A{range_start}"
            else:
                contig += f"A{range_start}-{index}"
            range_start = None
    return contig

def motif_indices_to_fixed_positions(motif_indices: Union[str, List]) -> str:
    """Converts motif indices to the fixed positions string format compatible with ProteinMPNN.
    e.g. [1, 2, 3, 4, 5, 8, 9] -> "1 2 3 4 5 8 9"
    
    Args:
        motif_indices (Union[str, List]): List-like motif indices.

    Returns:
        fix_positions (str): A str containing fixed positions seperated by space.
    """
    # Converts motif indices to the fixed positions string format
    if isinstance(motif_indices, list):
        motif_indices = str(motif_indices)
    motif_indices = motif_indices.strip('[]').split(', ')
    motif_indices = sorted([int(index) for index in motif_indices])
    fixed_positions = ' '.join(str(idx) for idx in motif_indices)
    return f"{fixed_positions}"


def parse_contig_string(contig_string):
    # Code by @blt2114
    contig_segments = []
    for motif_segment in contig_string.split(";"):
        segment_dict ={"chain":motif_segment[0]}
        if "-" in motif_segment:
            segment_dict["start"], segment_dict["end"] = motif_segment[1:].split("-")

        else:
            segment_dict["start"] = segment_dict["end"] = motif_segment[1:]
        contig_segments.append(segment_dict)
    return contig_segments


def introduce_redesign_positions(
    fix_positions: Union[List, str],
    redesign_positions: str
) -> List:
    """Adjust fixed positions list to make certain positions redesignable.

    Args:
        fix_positions (Union[List, str]): Fixed positions
        redesign_positions (str): Redesigned list from "redesign_positions" returned by `get_csv_data()`. 

    Returns:
        List: A list of fixed positions for ProteinMPNN, laterly as input for `motif_indices_to_fixed_positions`.
    """
    if isinstance(fix_positions, list):
        fix_positions = str(fix_positions)
    
    pos_to_redesign = parse_contig_string(redesign_positions)
    redesign_pos = []
    for seg in pos_to_redesign:
        for i in range(int(seg['start']), int(seg['end']) + 1):
            redesign_pos.append(int(i))
    final_pos = [pos for pos in eval(fix_positions) if pos not in redesign_pos]
    return final_pos

def generate_indices_and_mask(contig: str) -> Tuple[int, List[int], np.ndarray]:
    """Index motif and scaffold positions by contig for sequence redesign.
    Args:
        contig (str): A string containing positions for scaffolds and motifs.
        
        Details:
        Scaffold parts: Contain a single integer.
        Motif parts: Start with a letter (chain ID) and contain either a single positions (e.g. A33) or a range of positions (e.g. A33-39).
        The numbers following chain IDs corresponds to the motif positions in native backbones, which are used to calculate motif reconstruction later on. 
        e.g. "15/A45-65/20/A20-30"
        NOTE: The scaffold part should be DETERMINISTIC in this case as it contains information for the corresponding protein backbones. 

    Raises:
        ValueError: Once a "-" is detected in scaffold parts, throws an error for the aforementioned reason.

    Returns:
        A Tuple containing:
            - overall_length (int): Total length of the sequence defined by the contig.
            - motif_indices (List[int]): List of indices where motifs are located.
            - motif_mask (np.ndarray): Boolean array where True indicates motif positions and False for scaffold positions.
    """
    ALPHABET = "ABCDEFGHJKLMNOPQRSTUVWXYZ"
    components = contig.split('/')
    current_position = 1  # Start positions at 1 for 1-based indexing
    motif_indices = []
    motif_mask = []

    for part in components:
        if part[0] in ALPHABET:
            # Motif part
            if '-' in part:
                start, end = map(int, part[1:].split("-"))
            else: # Single motif
                start = end = int(part[1:])
            length = (end - start + 1)
            motif_indices.extend(range(current_position, current_position + length))
            motif_mask.extend([True] * length)
        else:
            # Scaffold part
            if '-' in part:
                raise ValueError(f'There is "-" in scaffold {part}, which supposed to be determined already! Please check again.')
            length = int(part)
            motif_mask.extend([False] * length)
        
        current_position += length  # Update the current position after processing each part

    # Convert motif_mask to a numpy array for more efficient boolean operations
    motif_mask = np.array(motif_mask, dtype=bool)
    overall_length = motif_mask.shape[0]

    return (overall_length, motif_indices, motif_mask)


def get_redesign_positions(pdb_path: Union[str, Path]) -> Tuple[List[int], List[str]]:
    """Index residues to be redesigned by "UNK" residues.

    Args:
        pdb_path (Union[str, Path]): File path for input PDB file.

    Returns:
        Information for redesign positions
          redesign_positions (List[int]): Indices of positions to be redesigned inside biotite array object.
          redesign_chain_pos (List[str]): Indices integrated with chain information. Useful for further information storing.
    """
    all_atom_array = strucio.load_structure(pdb_path)
    ca_array = strucio.load_structure(all_atom_array[(all_atom_array.atom_name=="CA")]) # Get C-alpha array for convenience of indexing
    
    # Get three lists to iterate
    res_id_list = ca_array.res_id
    chain_id_list = ca_array.chain_id
    res_name_list = ca_array.res_name
    
    redesign_positions = [index for index, resname in zip(res_id_list, res_name_list) if resname == "UNK"]
    redesign_chain_positions = [f"{chain_id_list[idx]}{res_id_list[idx]}" for idx, resname in enumerate(res_name_list) if resname == "UNK"]
    
    return(redesign_positions, redesign_chain_positions)


def read_contig_from_header(pdb_path: Union[str, Path]) -> Optional[Tuple[str, str]]:
    """Read contig information from HEADER record of PDB file.
    By default, the contig info is stored at the "classification" section and
    file name is stored at the "identifier" section.

    Args:
        pdb_path (Union[str, Path]): Path of input PDB file.

    Returns:
        contig (str): The contig information for where motifs and scaffolds are placed.
        identifier (str): The file name of input PDB file.
        
        If HEADER couldn't be parsed, then it's either because problem in format or
        you use csv instead of HEADER to load contig information. Return nothing in this case.
    """
    try:
        header_info = parsePDBHeader(pdb_path)
        contig = header_info['classification']
        file_identifier = header_info['identifier']
        return contig, file_identifier
    except KeyError as e:
        logging.warning(f"The HEADER of {pdb_path} could not be parsed properly. Please make sure the format is right\
            or you are using csv file to provide information for contig.")
        return None


def write_contig_into_header(
    pdb_path: Union[str, Path],
    contig: str,
    write_additional_info: bool = True
):

    date = datetime.now().strftime("%d-%b-%y").upper() if write_additional_info else ""
    #identifier = os.path.basename(pdb_path).strip('.pdb') if write_additional_info else ""
    header_string = f"HEADER    {contig:<40}{date:>9}\n"
    
    with open(pdb_path, "r") as f:
        file_lines = f.readlines()
        file_lines.insert(0, header_string)
    with open('test.pdb', "w") as f:
        f.writelines(file_lines)