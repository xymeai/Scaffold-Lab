import os
import tree
import time
import numpy as np
import hydra
import torch
import subprocess
import logging
import pandas as pd
import sys
import shutil
import GPUtil
import rootutils
from pathlib import Path
from typing import *
from omegaconf import DictConfig, OmegaConf

import esm
from biotite.sequence.io import fasta

path = rootutils.find_root(search_from='./', indicator=[".git", "setup.cfg"])
rootutils.set_root(
    path=path, # path to the root directory
    project_root_env_var=True, # set the PROJECT_ROOT environment variable to root directory
    dotenv=True, # load environment variables from .env if exists in root directory
    pythonpath=True, # add root directory to the PYTHONPATH (helps with imports)
    cwd=True, # change current working directory to the root directory (helps with filepaths)
)

from analysis import utils as au
from data import structure_utils as su


class Refolder:

    """
    Perform refolding analysis on a set of protein backbones.
    Organized by the following steps:
    1. Initialization and config reading
    2. Run ProteinMPNN on a given set of PDB files
    3. Run ESMFold on sequences generated by ProteinMPNN ()
    4. Calculate the metrics (RMSD, TM-score, pLDDT, etc.) and write information into a csv file.
    
    One can also modify this script to perform fixed backbone design and evaluations on refoldability.
    Adapted from https://github.com/jasonkyuyim/se3_diffusion/blob/master/experiments/inference_se3_diffusion.py
    """
    
    def __init__(
        self,
        conf:DictConfig,
        conf_overrides: Dict=None
        ):
        
        self._log = logging.getLogger(__name__)
        
        OmegaConf.set_struct(conf, False)
        
        self._conf = conf
        self._infer_conf = conf.inference
        self._sample_conf = self._infer_conf.samples
        
        self._rng = np.random.default_rng(self._infer_conf.seed)
        
        # Set-up accelerator
        if torch.cuda.is_available():
            if self._infer_conf.gpu_id is None:
                available_gpus = ''.join(
                    [str(x) for x in GPUtil.getAvailable(
                        order="memory", limit = 8)]
                )
                self.device = f'cuda:{available_gpus[0]}'
            else:
                self.device = f'cuda:{self._infer_conf.gpu_id}'
        else:
            self.device = 'cpu'
        self._log.info(f'Using device: {self.device}')
        
        
        # Set-up directories
        output_dir = self._infer_conf.output_dir
        
        self._output_dir = output_dir
        os.makedirs(self._output_dir, exist_ok=True)
        self._pmpnn_dir = self._infer_conf.pmpnn_dir
        self._sample_dir = self._infer_conf.backbone_pdb_dir
        self._CA_only = self._infer_conf.CA_only
        
        # Save config
        config_folder = os.path.basename(Path(self._output_dir))
        config_path = os.path.join(self._output_dir, f"{config_folder}.yaml")
        with open(config_path, 'w') as f:
            OmegaConf.save(config=self._conf, f=f)
        self._log.info(f'Saving self-consistency config to {config_path}')
        
        # Load models and experiment
        if 'cuda' in self.device:
            self._folding_model = esm.pretrained.esmfold_v1().eval()
        elif self.device == 'cpu': # ESMFold is not supported for half-precision model when running on CPU
            self._folding_model = esm.pretrained.esmfold_v1().float().eval()
        self._folding_model = self._folding_model.to(self.device)
    
    def run_sampling(self):
        
        # Run ProteinMPNN

        for pdb_file in os.listdir(self._sample_dir):
            backbone_name = os.path.splitext(pdb_file)[0]
            print(f'sample_dir: {self._sample_dir}')
            basename_dir = os.path.basename(os.path.normpath(self._sample_dir))
            backbone_dir = os.path.join(self._output_dir, basename_dir, f'{backbone_name}')
            if os.path.exists(backbone_dir):
                continue
            
            os.makedirs(backbone_dir, exist_ok=True)
            self._log.info(f'Running self-consistency on {backbone_name}')
            print(f'pdb_file:{pdb_file}')
            print(f'backbone_dir:{backbone_dir}')
            shutil.copy2(os.path.join(self._sample_dir, pdb_file), backbone_dir)
            self._log.info(f'copied {pdb_file} to {backbone_dir}')
            
            #seperate_pdb_folder = os.path.join(backbone_dir, backbone_name)
            pdb_path = os.path.join(backbone_dir, pdb_file)
            sc_output_dir = os.path.join(backbone_dir, 'self_consistency')
            os.makedirs(sc_output_dir, exist_ok=True)
            shutil.copy(pdb_path, os.path.join(
                sc_output_dir, os.path.basename(pdb_path)))
            _ = self.run_self_consistency(
                sc_output_dir,
                pdb_path,
                motif_mask=None
            )
            self._log.info(f'Done sample: {pdb_path}')
    
    def run_self_consistency(
            self,
            decoy_pdb_dir: str,
            reference_pdb_path: str,
            motif_mask: Optional[np.ndarray]=None):
        """Run self-consistency on design proteins against reference protein.
        
        Args:
            decoy_pdb_dir: directory where designed protein files are stored.
            reference_pdb_path: path to reference protein file
            motif_mask: Optional mask of which residues are the motif.

        Returns:
            Writes ProteinMPNN outputs to decoy_pdb_dir/seqs
            Writes ESMFold outputs to decoy_pdb_dir/esmf
            Writes results in decoy_pdb_dir/sc_results.csv
        """

        # Run ProteinMPNN
        
        jsonl_path = os.path.join(decoy_pdb_dir, "parsed_pdbs.jsonl")
        process = subprocess.Popen([
            'python',
            f'{self._pmpnn_dir}/helper_scripts/parse_multiple_chains.py',
            f'--input_path={decoy_pdb_dir}',
            f'--output_path={jsonl_path}',
        ])
        
        _ = process.wait()
        num_tries = 0
        ret = -1
        pmpnn_args = [
            sys.executable,
            f'{self._pmpnn_dir}/protein_mpnn_run.py',
            '--out_folder',
            decoy_pdb_dir,
            '--jsonl_path',
            jsonl_path,
            '--num_seq_per_target',
            str(self._sample_conf.seq_per_sample),
            '--sampling_temp',
            '0.1',
            '--seed',
            '33',
            '--batch_size',
            '1',
        ]
        if self._infer_conf.gpu_id is not None:
            pmpnn_args.append('--device')
            pmpnn_args.append(str(self._infer_conf.gpu_id))
        if self._CA_only == True:
            pmpnn_args.append('--ca_only')
        
        while ret < 0:
            try:
                process = subprocess.Popen(
                    pmpnn_args,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.STDOUT
                )
                ret = process.wait()
            except Exception as e:
                num_tries += 1
                self._log.info(f'Failed ProteinMPNN. Attempt {num_tries}/5')
                torch.cuda.empty_cache()
                if num_tries > 4:
                    raise e
        mpnn_fasta_path = os.path.join(
            decoy_pdb_dir,
            'seqs',
            os.path.basename(reference_pdb_path).replace('.pdb', '.fa')
        )

        # Run ESMFold on each ProteinMPNN sequence and calculate metrics.
        mpnn_results = {
            'tm_score': [],
            'sample_path': [],
            'header': [],
            'sequence': [],
            'rmsd': [],
            'pae': [],
            'ptm': [],
            'plddt': [],
            'length': [],
            'mpnn_score': []
        }
        if motif_mask is not None:
            # Only calculate motif RMSD if mask is specified.
            mpnn_results['motif_rmsd'] = []
        esmf_dir = os.path.join(decoy_pdb_dir, 'esmf')
        os.makedirs(esmf_dir, exist_ok=True)
        fasta_seqs = fasta.FastaFile.read(mpnn_fasta_path)
        
        # Only take seqs with lowerst global score to do refolding analysis
        scores = []
        for i, (header, string) in enumerate(fasta_seqs.items()):
            if i == 0:
                global_score = float(header.split(", ")[2].split("=")[1])
                original_seq = (global_score, header, string)
            else: 
                global_score = float(header.split(", ")[3].split("=")[1])
                scores.append((global_score, header, string))
        scores.sort(key=lambda x: x[0])
        top_seqs = scores[:10]
        top_seqs.insert(0, original_seq) # Include the original seq
        
        sample_feats = su.parse_pdb_feats('sample', reference_pdb_path)
        for i, (mpnn_score, header, string) in enumerate(top_seqs):

            # Run ESMFold
            self._log.info(f'Running ESMfold......')
            esmf_sample_path = os.path.join(esmf_dir, f'sample_{i}.pdb')
            _, full_output = self.run_folding(string, esmf_sample_path)
            esmf_feats = su.parse_pdb_feats('folded_sample', esmf_sample_path)
            sample_seq = su.aatype_to_seq(sample_feats['aatype'])

            # Calculate scTM of ESMFold outputs with reference protein
            _, tm_score = su.calc_tm_score(
                sample_feats['bb_positions'], esmf_feats['bb_positions'],
                sample_seq, sample_seq)
            rmsd = su.calc_aligned_rmsd(
                sample_feats['bb_positions'], esmf_feats['bb_positions'])
            pae = torch.mean(full_output['predicted_aligned_error']).item()
            ptm = full_output['ptm'].item()
            plddt = full_output['mean_plddt'].item()
            if motif_mask is not None:
                sample_motif = sample_feats['bb_positions'][motif_mask]
                of_motif = esmf_feats['bb_positions'][motif_mask]
                motif_rmsd = su.calc_aligned_rmsd(
                    sample_motif, of_motif)
                mpnn_results['motif_rmsd'].append(f'{motif_rmsd:.3f}')
            mpnn_results['rmsd'].append(f'{rmsd:.3f}')
            mpnn_results['tm_score'].append(f'{tm_score:.3f}')
            mpnn_results['sample_path'].append(esmf_sample_path)
            mpnn_results['header'].append(header)
            mpnn_results['sequence'].append(string)
            mpnn_results['pae'].append(f'{pae:.3f}')
            mpnn_results['ptm'].append(f'{ptm:.3f}')
            mpnn_results['plddt'].append(f'{plddt:.3f}')
            mpnn_results['length'].append(len(string))
            mpnn_results['mpnn_score'].append(f'{mpnn_score:.3f}')

        # Save results to CSV
        csv_path = os.path.join(decoy_pdb_dir, 'sc_results.csv')
        mpnn_results = pd.DataFrame(mpnn_results)
        mpnn_results.to_csv(csv_path)

    def run_folding(self, sequence, save_path):
        """
        Run ESMFold on sequence.
        TBD: Add options for OmegaFold and AlphaFold2.
        """
        with torch.no_grad():
            output = self._folding_model.infer(sequence)
            output_dict = {key: value.cpu() for key, value in output.items()}
            output = self._folding_model.output_to_pdb(output)
        with open(save_path, "w") as f:
            f.write(output[0])
        return output, output_dict  
    
@hydra.main(version_base=None, config_path="../../config", config_name="unconditional")
def run(conf: DictConfig) -> None:
    
    print('Starting refolding for unconditional generation......')
    start_time = time.time()
    refolder = Refolder(conf)
    refolder.run_sampling()
    elapsed_time = time.time() - start_time
    print(f"Finished in {elapsed_time:.2f}s. Voila!")
    
if __name__ == '__main__':
    run()
