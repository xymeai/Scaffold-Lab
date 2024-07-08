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
import rootutils
import shutil
import GPUtil
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
    1. Config initialization
    2. Run ProteinMPNN on a given set of PDB files
    3. Run ESMFold/AlphaFold2 on sequences generated by ProteinMPNN ()
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

        # Sanity check
        if self._sample_conf.seq_per_sample < self._sample_conf.mpnn_batch_size:
            raise ValueError(f'Sequences per sample {self._sample_conf.seq_per_sample} < \
            batch size {self._sample_conf.mpnn_batch_size}!')
        
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

        # Customizing different structure prediction methods 
        self._forward_folding = self._infer_conf.predict_method
        if 'AlphaFold2' in self._forward_folding:
            self._af2_conf = self._infer_conf.af2
            colabfold_path = self._af2_conf.executive_colabfold_path
            current_path = os.environ.get('PATH', '')
            os.environ['PATH'] = colabfold_path + ":" + current_path
            if self.device == 'cpu':
                raise ValueError("Only GPU supported for AlphaFold2 currently.")
        
        
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
                self._log.info(f'Backbone {backbone_name} already existed, pass then.')
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
            str(self._sample_conf.mpnn_batch_size),
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
        af2_raw_dir = os.path.join(decoy_pdb_dir, 'af2_raw_outputs')
        fasta_seqs = fasta.FastaFile.read(mpnn_fasta_path)
        
        if self._sample_conf.sort_by_score:
        # Only take seqs with lowerst global score to enter refolding
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
            top_seqs_path = os.path.join(
                decoy_pdb_dir,
                'seqs',
                f'top_score_{os.path.basename(reference_pdb_path)}'.replace('.pdb', '.fa')
            )
            _ = au.write_seqs_to_fasta(top_seqs, top_seqs_path)
        
        seqs_to_refold = top_seqs_path if self._sample_conf.sort_by_score else mpnn_fasta_path
        seqs_dict = fasta.FastaFile.read(seqs_to_refold)
        
        sample_feats = su.parse_pdb_feats('sample', reference_pdb_path)
        if 'ESMFold' in self._forward_folding:
            os.makedirs(esmf_dir, exist_ok=True)
            for i, (header, string) in enumerate(seqs_dict.items()):
                
                # Get score for ProteinMPNN
                if header.startswith("T=0"):
                    idx = header.split('sample=')[1].split(',')[0]
                    score = float(header.split(", ")[3].split("=")[1])
                else:
                    idx = 0
                    score = float(header.split(", ")[2].split("=")[1])

                # Run ESMFold
                self._log.info(f'Running ESMFold......')
                esmf_sample_path = os.path.join(esmf_dir, f'sample_{idx}.pdb')
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
                    esm_motif = esmf_feats['bb_positions'][motif_mask]
                    motif_rmsd = su.calc_aligned_rmsd(
                        sample_motif, esm_motif)
                    mpnn_results['motif_rmsd'].append(f'{motif_rmsd:.3f}')
                mpnn_results['rmsd'].append(f'{rmsd:.3f}')
                mpnn_results['tm_score'].append(f'{tm_score:.3f}')
                mpnn_results['sample_path'].append(os.path.abspath(esmf_sample_path))
                mpnn_results['header'].append(header)
                mpnn_results['sequence'].append(string)
                mpnn_results['pae'].append(f'{pae:.3f}')
                mpnn_results['ptm'].append(f'{ptm:.3f}')
                mpnn_results['plddt'].append(f'{plddt:.3f}')
                mpnn_results['length'].append(len(string))
                mpnn_results['mpnn_score'].append(f'{score:.3f}')

            # Save results to CSV
            esm_csv_path = os.path.join(decoy_pdb_dir, 'esm_eval_results.csv')
            mpnn_results = pd.DataFrame(mpnn_results)
            esm_columns = ['sample_idx'] + [c for c in mpnn_results.columns if c != 'sample_idx']
            mpnn_results = mpnn_results.reindex(columns=esm_columns)
            mpnn_results.to_csv(esm_csv_path, index=False)

        # Run AF2
        if 'AlphaFold2' in self._forward_folding:
            self._log.info(f'Running AlphaFold2......')

            _ = self.run_af2(seqs_to_refold, af2_raw_dir)
            af2_dir = os.path.join(decoy_pdb_dir, 'af2')
            os.makedirs(af2_dir, exist_ok=True)
            af2_outputs = au.cleanup_af2_outputs(
                af2_raw_dir,
                os.path.join(decoy_pdb_dir, 'af2')
            )

            for i, (header, string) in enumerate(seqs_dict.items()):
                # Find index and score
                if header.startswith("T=0"):
                    idx = header.split('sample=')[1].split(',')[0]
                    score = float(header.split(", ")[3].split("=")[1])
                else:
                    idx = 0
                    score = float(header.split(", ")[2].split("=")[1])

                af2_sample_path = os.path.join(af2_dir, f'sample_{idx}.pdb')
                af2_feats = su.parse_pdb_feats('folded_sample', af2_sample_path)
                sample_seq = su.aatype_to_seq(sample_feats['aatype'])

                # Calculation
                _, tm_score = su.calc_tm_score(
                    sample_feats['bb_positions'], af2_feats['bb_positions'],
                    sample_seq, sample_seq)
                rmsd = su.calc_aligned_rmsd(
                    sample_feats['bb_positions'], af2_feats['bb_positions'])
                if motif_mask is not None:
                    sample_motif = sample_feats['bb_positions'][motif_mask]
                    af2_motif = af2_feats['bb_positions'][motif_mask]
                    motif_rmsd = su.calc_aligned_rmsd(
                        sample_motif, af2_motif)
                af2_outputs[f'sample_{idx}']['rmsd'] = f'{rmsd:.3f}'
                af2_outputs[f'sample_{idx}']['tm_score'] = f'{tm_score:.3f}'
                af2_outputs[f'sample_{idx}']['header'] = header
                af2_outputs[f'sample_{idx}']['sequence'] = string
                af2_outputs[f'sample_{idx}']['length'] = len(string)
                af2_outputs[f'sample_{idx}']['mpnn_score'] = f'{score:.3f}'
                #af2_outputs[f'sample_{i}']['mpnn_score'] = 
            print(f'final_outputs: {af2_outputs}')
            af2_csv_path = os.path.join(decoy_pdb_dir, 'af2_eval_results.csv')
            af2_df = pd.DataFrame.from_dict(af2_outputs, orient='index')
            af2_df.reset_index(inplace=True)
            af2_df.rename(columns={'index': 'sample'}, inplace=True)
            af2_df.drop('sample', axis=1, inplace=True)
            af2_df['sample_idx'] = af2_df['sample_idx'].astype(int)
            af2_columns = ['sample_idx'] + [c for c in af2_df.columns if c != 'sample_idx']
            af2_df = af2_df.reindex(columns=af2_columns)
            af2_df.sort_values('sample_idx', inplace=True)
            af2_df.to_csv(af2_csv_path, index=False)

        if 'ESMFold' and 'AlphaFold2' in self._forward_folding:
            esm_results = pd.read_csv(esm_csv_path)
            af2_results = pd.read_csv(af2_csv_path)
            esm_results['folding_method'] = 'ESMFold'
            af2_results['folding_method'] = 'AlphaFold2'
            joint_results = pd.concat([esm_results, af2_results], ignore_index=True)
            joint_results.to_csv(os.path.join(decoy_pdb_dir, 'joint_eval_results.csv'), index=False)




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

    def run_af2(self, sequence, save_path):
        """
        Run AlphaFold2 (single-sequence) through LocalColabFold.
        """

        #_ = process.wait()
        num_tries_af2 = 0
        ret_af2 = -1

        # Setting AF2 args
        af2_args = [
            #sys.executable,
            'colabfold_batch',
            sequence,
            save_path,
            '--msa-mode',
            'single_sequence',
            '--num-recycle',
            str(self._af2_conf.recycle),
            '--random-seed',
            str(self._af2_conf.seed),
            '--model-type',
            self._af2_conf.model_type,
            '--num-models',
            str(self._af2_conf.num_models),
        ]
        if self._af2_conf.num_models > 1:
            af2_args.append('--rank')
            af2_args.append(self._af2_conf.rank)
        if self._af2_conf.use_amber_relax:
            af2_args.append('--amber')
            af2_args.append('--num-relax')
            af2_args.append(str(self._af2_conf.num_relax))
            if self._af2_conf.use_gpu_relax:
                af2_args.append('--use-gpu-relax')

        # Run AF2
        while ret_af2 < 0:
            try:
                process_af2 = subprocess.Popen(
                    af2_args,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.STDOUT
                )
                
                ret_af2 = process_af2.wait()
            except Exception as e:
                num_tries_af2 += 1
                self._log.info(f'Hmm...Maybe some error occurs during executing AlphaFold2. Tried {num_tries_af2}/5')
                torch.cuda.empty_cache()
                if num_tries_af2 > 10:
                    raise e

        # Handle outputs

    
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
