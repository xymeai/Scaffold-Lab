import os
import numpy as np
import pandas as pd
import subprocess
import argparse
import psutil
import time
import typing as T
from typing import Optional, Union, List, Tuple, Dict
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed

"""
Novelty Calculation.

We provide two options for calculating the pdbTM value:
1. Indepentdent Mode: Calculate the pdbTM value of a single input PDB file.
    Example usage: python pdbTM.py -c {example}.pdb
2. Batch Mode: Calculate pdbTM of a number of PDBs once in a time. 
    Take a csv file with 'backbone_path' as the path of PDB files as input.
    A csv file will be returned with 'pdbTM' column filled with corresponding values.
    
Args:

Independent Mode:
[Required]
'-c', '--calculate': Path of PDB file you want to calculate pdbTM value of.

Batch Mode:
[Required]
'-i', '--input': Path of input csv file you want to calculate with.
[Optional]
'-o', '--output': Path of output csv file with calculated pdbTM values.
                    Default = "novelty_results.csv" 
"""

def pdbTM(
    input: Union[str, Path],
    foldseek_database_path: Union[str, Path],
    process_id: int,
    save_tmp: bool = False,
    foldseek_path: Optional[Union[Path, str]] = None,
) -> Union[float, dict]:
    """
    Calculate pdbTM values with a customized set of parameters by Foldseek.
    
    Args:
    `input`: Input PDB file or csv file containing PDB paths.
    `process_id`: Used for saving temporary files generated by Foldseek.
    `save_tmp`: If True, save tmp files generated by Foldseek, otherwise deleted after calculation.
    `foldseek_path`: Path of Foldseek binary file for executing the calculations.
                     If you've already installed Foldseek through conda, just use "foldseek"
                     instead of this path.
                     
    CMD args:
    `pdb100`: Path of PDB database created compatible with Foldseek format.
    `output_file`: .m8 file containing Foldseek search results. Deleted if `save_tmp` = False.
    `tmp`: Temporary path when running Foldseek.
    For other CMD parameters and usage, we suggest users go to Foldseek official website
    (https://github.com/steineggerlab/foldseek) or type `foldseek easy-search -h` for detailed information.
    
    Returns:
    `top_pdbTM`: The highest pdbTM value among the top three targets hit by Foldseek.
    """
    # Handling multiprocessing
    base_tmp_path = "../tmp/"
    tmp_path = os.path.join(base_tmp_path, f'process_{process_id}')
    os.makedirs(tmp_path, exist_ok=True)
    
    #pdb100 = "~/zzq/foldseek/database/pdb100/pdb"
    # Check whether input is a directory or a single file
    if ".pdb" in input:
        output_file = f'./{os.path.basename(input)}.m8'
        
        cmd = f'foldseek easy-search \
                {input} \
                {foldseek_database_path} \
                {output_file} \
                {tmp_path} \
                --format-mode 4 \
                --format-output query,target,evalue,alntmscore,rmsd,prob \
                --alignment-type 1 \
                --num-iterations 2 \
                -e inf'
                
        if foldseek_path is not None:
            cmd.replace('foldseek', {foldseek_path})
            
        subprocess.run(cmd, shell=True, check=True)
        
        result = pd.read_csv(output_file, sep='\t')
        top_pdbTM = round(result['alntmscore'].head(1).max(), 3)
        
        if save_tmp == False:
            os.remove(output_file)
            
    else:
        return None
            
    return top_pdbTM

def calculate_novelty(
    input_csv: Union[str, Path, pd.DataFrame],
    foldseek_database_path: Union[str, Path],
    max_workers: int,
    cpu_threshold: float 
) -> pd.DataFrame:
    df = pd.read_csv(input_csv) if isinstance(input_csv, str) or isinstance(input_csv, Path) else input_csv
    if 'pdbTM' not in df.columns:
        df['pdbTM'] = None
        
    futures = {}
    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        process_id = 0
        for backbone_path in df['backbone_path'].unique():
            if pd.isna(df[df['backbone_path'] == backbone_path]['pdbTM'].iloc[0]):
                while psutil.cpu_percent(interval=1) > cpu_threshold:
                    time.sleep(0.5)
                future = executor.submit(pdbTM, backbone_path, foldseek_database_path, process_id)
                futures[future] = backbone_path
                process_id += 1
                
        for future in as_completed(futures):
            pdbTM_value = future.result()
            backbone_path = futures[future]
            df.loc[df['backbone_path'] == backbone_path, 'pdbTM'] = pdbTM_value
        
        #df['pdbTM'] = df['backbone_path'].apply(lambda x: pdbTM_values[x])
    
    return df

def create_parser():
    parser = argparse.ArgumentParser(description='Calculating pdb-TM(novelty) for protein backbones')
    parser.add_argument(
        '-i',
        '--input',
        type=str,
        help='Input csv file'
    )
    parser.add_argument(
        '-c',
        '--calculate',
        type=str,
        help='Input pdb file to calculate pdb-TM independently'
    )
    parser.add_argument(
        '-o',
        '--output',
        type=str,
        default='novelty_results.csv',
        help='Output csv file',
    )
    return parser
    
if __name__ == "__main__":
    parser = create_parser()
    args = parser.parse_args()
    
    if args.input and args.calculate is not None:
        raise ValueError('Cannot read csv file and single PDB file simultaneously!')
    
    if args.input is not None:
        # Check hardware status
        num_cpu_cores = os.cpu_count()
        
        results = calculate_novelty(
            input_csv=args.input,
            max_workers=num_cpu_cores,
            cpu_threshold=75.0
        )
        results.to_csv(args.output, index=False)
    
    if args.calculate is not None:
        value = pdbTM(args.calculate)
        print(f'TM-Score between {os.path.basename(args.calculate)} and its closest protein in PDB is {value}.')