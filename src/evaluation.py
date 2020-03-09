import msprime
import tsinfer
import tskit

import argparse
from itertools import combinations
import logging
import math
import multiprocessing
import os
import random
import subprocess
import shutil
import sys
import tempfile

import seaborn as sns
import matplotlib.pyplot as plt

import numpy as np
import pandas as pd
import scipy
from scipy.stats import gaussian_kde
from sklearn.metrics import mean_squared_error, mean_squared_log_error

import tsdate # NOQA

relate_executable = os.path.join('tools', 'relate',
                                 'bin', 'Relate')
relatefileformat_executable = os.path.join('tools', 'relate',
                                           'bin', 'RelateFileFormats')
geva_executable = os.path.join('tools', 'geva', 'geva_v1beta')
geva_hmm_initial_probs = os.path.join('tools', 'geva', 'hmm', 'hmm_initial_probs.txt')
geva_hmm_emission_probs = os.path.join('tools', 'geva', 'hmm', 'hmm_emission_probs.txt')
tsinfer_executable = os.path.join('src', 'run_tsinfer.py')
tsdate_executable = os.path.join('src', 'run_tsdate.py')

TSDATE = "tsdate"
RELATE = "Relate"
GEVA = "GEVA"

"""
Code to run simulations testing accuracy of tsdate

Run:
python date.py
"""


def make_no_errors(g, error_prob):
    assert error_prob == 0
    return g


def make_seq_errors_simple(g, error_prob):
    """
    """
    raise NotImplementedError


def make_seq_errors_genotype_model(g, error_probs):
    """
    Given an empirically estimated error probability matrix, resample for a
    particular variant. Determine variant frequency and true genotype
    (g0, g1, or g2), then return observed genotype based on row in error_probs
    with nearest frequency. Treat each pair of alleles as a diploid individual.
    """
    m = g.shape[0]
    frequency = np.sum(g) / m
    closest_row = (error_probs['freq'] - frequency).abs().argsort()[:1]
    closest_freq = error_probs.iloc[closest_row]

    w = np.copy(g)

    # Make diploid (iterate each pair of alleles)
    genos = np.reshape(w, (-1, 2))

    # Record the true genotypes (0, 0=>0; 1, 0=>1; 0, 1=>2, 1, 1=>3)
    count = np.sum(np.array([1, 2]) * genos, axis=1)

    base_genotypes = np.array([[0, 0], [1, 0], [0, 1], [1, 1]])

    genos[count == 0, :] = base_genotypes[
        np.random.choice(
            4, sum(count == 0),
            p=closest_freq[['p00', 'p01', 'p01', 'p02']]
            .values[0] * [1, 0.5, 0.5, 1]), :]
    genos[count == 1, :] = base_genotypes[[0, 1, 3], :][
        np.random.choice(3, sum(count == 1),
                         p=closest_freq[['p10', 'p11', 'p12']].values[0]), :]
    genos[count == 2, :] = base_genotypes[[0, 2, 3], :][
        np.random.choice(3, sum(count == 2),
                         p=closest_freq[['p10', 'p11', 'p12']].values[0]), :]
    genos[count == 3, :] = base_genotypes[
        np.random.choice(4, sum(count == 3),
                         p=closest_freq[['p20', 'p21', 'p21', 'p22']]
                         .values[0]*[1, 0.5, 0.5, 1]), :]

    return np.reshape(genos, -1)


def generate_samples(
        ts, fn, aa_error="0", seq_error="0", empirical_seq_err_name=""):
    """
    Generate a samples file from a simulated ts. We can pass an integer or a
    matrix as the seq_error.
    If a matrix, specify a name for it in empirical_seq_err
    """
    record_rate = logging.getLogger().isEnabledFor(logging.INFO)
    n_variants = bits_flipped = bad_ancestors = 0
    assert ts.num_sites != 0
    fn += ".samples"
    sample_data = tsinfer.SampleData(path=fn,
                                     sequence_length=ts.sequence_length)

    # Setup the sequencing error used.
    # Empirical error should be a matrix not a float
    if not empirical_seq_err_name:
        seq_error = float(seq_error) if seq_error else 0
        if seq_error == 0:
            record_rate = False  # no point recording the achieved error rate
            sequencing_error = make_no_errors
        else:
            logging.info("Adding genotyping error: {} used in file {}".format(
                seq_error, fn))
            sequencing_error = make_seq_errors_simple
    else:
        logging.info("Adding empirical genotyping error: {} used in file {}"
                     .format(empirical_seq_err_name, fn))
        sequencing_error = make_seq_errors_genotype_model
    # Setup the ancestral state error used
    aa_error = float(aa_error) if aa_error else 0
    aa_error_by_site = np.zeros(ts.num_sites, dtype=np.bool)
    if aa_error > 0:
        assert aa_error <= 1
        n_bad_sites = round(aa_error * ts.num_sites)
        logging.info("""Adding ancestral allele polarity error:
                        {}% ({}/{} sites) used in file {}"""
                     .format(aa_error * 100, n_bad_sites, ts.num_sites, fn))
        # This gives *exactly* a proportion aa_error or bad sites
        # NB - to to this probabilitistically,
        # use np.binomial(1, e, ts.num_sites)
        aa_error_by_site[0:n_bad_sites] = True
        np.random.shuffle(aa_error_by_site)
        assert sum(aa_error_by_site) == n_bad_sites
    for ancestral_allele_error, v in zip(aa_error_by_site, ts.variants()):
        n_variants += 1
        genotypes = sequencing_error(v.genotypes, seq_error)
        if record_rate:
            bits_flipped += np.sum(np.logical_xor(genotypes, v.genotypes))
            bad_ancestors += ancestral_allele_error
        if ancestral_allele_error:
            sample_data.add_site(
                position=v.site.position, alleles=v.alleles,
                genotypes=1 - genotypes)
        else:
            sample_data.add_site(
                position=v.site.position, alleles=v.alleles,
                genotypes=genotypes)
    if record_rate:
        logging.info(
            " actual error rate = {} over {} sites before {} ancestors flipped"
            .format(bits_flipped/(n_variants*ts.sample_size),
                    n_variants, bad_ancestors))

    sample_data.finalise()
    return sample_data


def run_neutral_sim(
        sample_size, Ne, length, mutation_rate, recombination_rate, seed=None):
    """
    Run simulation
    """
    ts = msprime.simulate(
        sample_size=sample_size, Ne=Ne, length=length,
        mutation_rate=mutation_rate, recombination_rate=recombination_rate,
        random_seed=seed)
    return ts


def out_of_africa(sample_size, mutation_rate, recombination_rate, length):
    # First we set out the maximum likelihood values of the various parameters
    # given in Table 1.
    N_A = 7300
    N_B = 2100
    N_AF = 12300
    N_EU0 = 1000
    N_AS0 = 510
    # Times are provided in years, so we convert into generations.
    generation_time = 25
    T_AF = 220e3 / generation_time
    T_B = 140e3 / generation_time
    T_EU_AS = 21.2e3 / generation_time
    # We need to work out the starting (diploid) population sizes based on
    # the growth rates provided for these two populations
    r_EU = 0.004
    r_AS = 0.0055
    N_EU = N_EU0 / math.exp(-r_EU * T_EU_AS)
    N_AS = N_AS0 / math.exp(-r_AS * T_EU_AS)
    # Migration rates during the various epochs.
    m_AF_B = 25e-5
    m_AF_EU = 3e-5
    m_AF_AS = 1.9e-5
    m_EU_AS = 9.6e-5
    # Population IDs correspond to their indexes in the population
    # configuration array. Therefore, we have 0=YRI, 1=CEU and 2=CHB
    # initially.
    population_configurations = [
        msprime.PopulationConfiguration(
            sample_size=sample_size // 3, initial_size=N_AF),
        msprime.PopulationConfiguration(
            sample_size=sample_size // 3, initial_size=N_EU, growth_rate=r_EU),
        msprime.PopulationConfiguration(
            sample_size=sample_size // 3, initial_size=N_AS, growth_rate=r_AS)
    ]
    migration_matrix = [
        [      0, m_AF_EU, m_AF_AS], # NOQA
        [m_AF_EU,       0, m_EU_AS],
        [m_AF_AS, m_EU_AS,       0],
    ]
    demographic_events = [
        # CEU and CHB merge into B with rate changes at T_EU_AS
        msprime.MassMigration(
            time=T_EU_AS, source=2, destination=1, proportion=1.0),
        msprime.MigrationRateChange(time=T_EU_AS, rate=0),
        msprime.MigrationRateChange(
            time=T_EU_AS, rate=m_AF_B, matrix_index=(0, 1)),
        msprime.MigrationRateChange(
            time=T_EU_AS, rate=m_AF_B, matrix_index=(1, 0)),
        msprime.PopulationParametersChange(
            time=T_EU_AS, initial_size=N_B, growth_rate=0, population_id=1),
        # Population B merges into YRI at T_B
        msprime.MassMigration(
            time=T_B, source=1, destination=0, proportion=1.0),
        # Size changes to N_A at T_AF
        msprime.PopulationParametersChange(
            time=T_AF, initial_size=N_A, population_id=0)
    ]
    # Use the demography debugger to print out the demographic history
    # that we have just described.
    ts = msprime.simulate(
        population_configurations=population_configurations,
        migration_matrix=migration_matrix,
        demographic_events=demographic_events, mutation_rate=mutation_rate,
        recombination_rate=recombination_rate, length=length)
    return ts




def return_vcf(tree_sequence, filename):
    with open("tmp/"+filename+".vcf", "w") as vcf_file:
        tree_sequence.write_vcf(vcf_file, ploidy=2)


def sampledata_to_vcf(sample_data, filename):
    """
    Input sample_data file, output VCF
    """

    num_individuals = len(sample_data.individuals_metadata[:])
    ind_list = list()
    pos_geno_dict = {"POS": list()}

    for i in range(int(num_individuals/2)):
        pos_geno_dict["msp_"+str(i)] = list()
        ind_list.append("msp_"+str(i))

    # add all the sample positions and genotypes
    for i in sample_data.genotypes():
        pos = int(round(sample_data.sites_position[i[0]]))
        if pos not in pos_geno_dict["POS"]:
            pos_geno_dict["POS"].append(pos)
            for j in range(0, len(i[1]), 2):
                pos_geno_dict["msp_" + str(int(j/2))].append(
                    str(i[1][j]) + "|" + str(i[1][j+1]))

    df = pd.DataFrame(pos_geno_dict)

    df["#CHROM"] = 1
    df["REF"] = "A"
    df["ALT"] = "T"
    df['ID'] = "."
    df['QUAL'] = "."
    df['FILTER'] = "PASS"
    df['INFO'] = "."
    df['FORMAT'] = "GT"

    cols = ['#CHROM', 'POS', 'ID', 'REF', 'ALT', 'QUAL', 'FILTER',
            'INFO', 'FORMAT']+ind_list
    df = df[cols]

    header = """##fileformat=VCFv4.2
##source=msprime 0.6.0
##FILTER=<ID=PASS, Description="All filters passed">
##contig=<ID=1, length=""" + str(int(sample_data.sequence_length)) + """>
##FORMAT=<ID=GT, Number=1, Type=String, Description="Genotype">
"""
    output_VCF = filename + ".vcf"
    with open(output_VCF, 'w') as vcf:
        vcf.write(header)

    df.to_csv(output_VCF, sep="\t", mode='a', index=False)
    return df




def compare_mutations(method_names, ts_list, relate_ages, geva_ages=None,
                      geva_positions=None):
    """
    Given a list of tree sequences, return a pandas dataframe with the age
    estimates for each mutation via each method (tsdate, tsinfer + tsdate,
    relate, geva etc.)

    :param list method_names: list of strings naming methods to be compared
    :param list ts_list: The list of tree sequences
    :param pandas.DataFrame geva_ages: mutation age estimates from geva
    :param pandas.DataFrame relate_ages: mutation age estimates from relate
    :return A DataFrame of mutations and age estimates from each method
    :rtype pandas.DataFrame
    """

    def get_mut_age(ts):
        """
        Get the edge id associated with each mutation
        """
        edge_diff_iter = ts.edge_diffs()
        right = 0
        edges_by_child = {}  # contains {child_node:edge_id}
        mut_age = {}
        for site in ts.sites():
            while right <= site.position:
                (left, right), edges_out, edges_in = next(edge_diff_iter)
                for e in edges_out:
                    del edges_by_child[e.child]
                for e in edges_in:
                    assert e.child not in edges_by_child
                    edges_by_child[e.child] = e.id
            for m in site.mutations:
                if m.node in edges_by_child:
                    edge_id = edges_by_child[m.node]
                    mut_age[site.position] = (ts.tables.nodes.time[m.node] +
                                              ts.tables.nodes.time[ts.edge(edge_id).parent])/2
        return mut_age
    relate_mut_ages = dict()
    if geva_ages is not None:
        geva_mut_ages = dict()

    ts = ts_list[0]
    dated_ts = ts_list[1]
    dated_inferred_ts = ts_list[2]

    mut_ages = get_mut_age(ts)
    mut_dated_ages = get_mut_age(dated_ts)
    mut_inferred_dated_ages = get_mut_age(dated_inferred_ts)

    for mut in ts.mutations():
        position = np.round(mut.position)

        if np.any(relate_ages['pos_of_snp'] == position):
            relate_row = relate_ages[relate_ages['pos_of_snp'] == position]
            relate_mut_ages[position] = (relate_row['age_end'] +
                                                 relate_row['age_begin']).values[0] / 2

        if geva_ages is not None:
            if position in geva_positions['Position'].values:
                geva_pos = geva_positions.index[
                    geva_positions['Position'] == position].tolist()[0]
                if geva_pos in geva_ages.index:
                    geva_mut_ages[position] = geva_ages.loc[geva_pos]['PostMean']

    if geva_ages is not None:
        run_results = pd.DataFrame(
            [mut_ages, mut_dated_ages, mut_inferred_dated_ages,
                relate_mut_ages, geva_mut_ages], index=[
                'simulated_ts', 'tsdate', 'tsdate_inferred', 'relate', 'geva']).T
    else:
        run_results = pd.DataFrame(
            [mut_ages, mut_dated_ages, mut_inferred_dated_ages, relate_mut_ages],
            index=['simulated_ts', 'tsdate', 'tsdate_inferred', 'relate']).T
    return run_results


def kc_distance_ts(ts_1, ts_2, lambda_param):
    ts_1_breakpoints = np.array(list(ts_1.breakpoints()))
    ts_2_breakpoints = np.array(list(ts_2.breakpoints()))
    comb_breakpoints = np.sort(np.unique(np.concatenate(
        [ts_1_breakpoints, ts_2_breakpoints])))
    comb_isin_1 = np.isin(comb_breakpoints, ts_1_breakpoints)
    comb_isin_2 = np.isin(comb_breakpoints, ts_2_breakpoints)
    ts_1_trees = ts_1.trees()
    ts_2_trees = ts_2.trees()
    kc = 0
    last_breakpoint = 0

    for index in range(len(comb_breakpoints)):
        try:
            if comb_isin_1[index]:
                tree_1 = next(ts_1_trees)
            if comb_isin_2[index]:
                tree_2 = next(ts_2_trees)

        except StopIteration:
            last_breakpoint = comb_breakpoints[index]
            break
        kc += tree_1.kc_distance(
            tree_2, lambda_param) * (comb_breakpoints[index + 1] -
                                     comb_breakpoints[index])
    kc /= (last_breakpoint - comb_breakpoints[0])
    return(kc)


def find_tmrcas_snps(ts_dict):
    """
    Find the tmrcas at each SNP (as in the Relate paper)
    """
    ts_true = ts_dict['ts']
    # tsdate_true = ts_dict['tsdate_true']
    # tsdate_inferred = ts_dict['tsdate_inferred']
    relate_ts = ts_dict['relate']

    if not all(ts.num_mutations == ts_true.num_mutations for ts in ts_dict.values()):
        print("tree sequences have unequal numbers of mutations")
        revised_sites = [int(round(val)) for val in ts_true.tables.sites.position]
        comparable_sites = ts_true.tables.sites.position[
            np.isin(np.array(revised_sites), relate_ts.tables.sites.position[:])]
    else:
        comparable_sites = ts_true.tables.sites.position
    comparable_sites = comparable_sites[np.random.choice(
        len(comparable_sites), len(comparable_sites)//10, replace=False)]

    if not all(ts.num_samples == ts_true.num_samples for ts in ts_dict.values()):
        raise("error, unequal number of samples")

    sample_pairs = combinations(np.arange(0, ts_true.num_samples), 2)
    sample_pairs = np.array(list(sample_pairs))
    sample_pairs = sample_pairs[np.random.choice(len(sample_pairs),
                                                 len(sample_pairs)//10, replace=False)]
    data = pd.DataFrame(columns=ts_dict.keys(), index=comparable_sites)
    data = np.zeros((len(ts_dict.keys()), len(comparable_sites)))

    for site_index, site in enumerate(comparable_sites):
        for method_index, (method, ts) in enumerate(ts_dict.items()):
            tree = ts.at(site)
            tmrcas = list()
            for pair_index, pair in enumerate(sample_pairs):
                tmrcas.append(tree.tmrca(pair[0], pair[1]))
            data[method_index, site_index] = np.mean(tmrcas)
    return(data)


def run_tsdate_old(ts, n, Ne, mut_rate, time_grid, grid_slices, estimation_method,
               approximate_prior):
    """
    Runs tsdate on true and inferred tree sequence
    Be sure to input HAPLOID effective population size
    """
    sample_data = tsinfer.formats.SampleData.from_tree_sequence(ts, use_times=False)
    inferred_ts = tsinfer.infer(sample_data).simplify()
    prior = tsdate.build_prior_grid(ts, timepoints=grid_slices,
                                    approximate_prior=approximate_prior)
    prior_inferred = tsdate.build_prior_grid(inferred_ts, timepoints=grid_slices,
                                             approximate_prior=approximate_prior)
    dated_ts = tsdate.date(ts, Ne, mutation_rate=mut_rate, prior=prior,
                           method=estimation_method)
    dated_inferred_ts = tsdate.date(inferred_ts, Ne, mutation_rate=mut_rate,
                                    prior=prior_inferred, method=estimation_method)
    # sample_data_wtimes = tsinfer.formats.SampleData.from_tree_sequence(
    #    ts, use_times=True)
    # inferred_ts_wtimes = tsinfer.infer(sample_data_wtimes)
    # tsdated_inferred_ts_wtimes = tsdate.date(inferred_ts_wtimes, Ne,
    #    mutation_rate=mut_rate, recombination_rate=None, grid_slices=grid_slices,
    #                          approximate_prior=approximate_prior)
    # redone_sample_data = tsinfer.formats.SampleData.from_tree_sequence(
    #    tsdated_ts, use_times=True)
    # inferred_ts_round2 = tsinfer.infer(redone_sample_data)
    # tsdated_ts_round2 = tsdate.date(inferred_ts_round2, Ne, mutation_rate=mut_rate,
    #    recombination_rate=None, grid_slices=grid_slices,
    #                          approximate_prior=approximate_prior)
    return dated_ts, inferred_ts, dated_inferred_ts


def construct_tsinfer_name(sim_name, subsample_size, input_seq_error=None):
    """
    Returns a TSinfer filename. In the future we may have a tweakable error parameter
    for tsinfer, which may be different from the actual error injected into the
    simulated samples, so we allow for this here.
    If the file is a subset of the original, this can be added to the
    basename in this function, or later using the
    add_subsample_param_to_name() routine.
    """
    d,f = os.path.split(sim_name)
    suffix = "" if input_seq_error is None else "SQerr{}".format(input_seq_error)
    name = os.path.join(d,'+'.join(['tsinfer', f, suffix]))
    if subsample_size is not None and not pd.isnull(subsample_size):
        name = add_subsample_param_to_name(name, subsample_size)
    return name

def run_tsdate(input_fn, Ne, mut_rate, timepoints, method):
    with tempfile.NamedTemporaryFile("w+") as ts_out:
        cmd = [sys.executable, tsdate_executable, input_fn, ts_out.name, str(Ne)]
        # cmd += ["--mutation-rate", str(mut_rate), "--timepoints", str(timepoints), "--method", str(method)]
        cpu_time, memory_use = time_cmd(cmd)
        dated_ts = tskit.load(ts_out.name)
    return dated_ts, cpu_time, memory_use


def run_tsinfer(sample_fn, length,
    num_threads=1, inject_real_ancestors_from_ts_fn=None, rho=None, error_probability=None):
    with tempfile.NamedTemporaryFile("w+") as ts_out:
        cmd = [sys.executable, tsinfer_executable, sample_fn, "--length", str(int(length))]
        cmd += ["--threads", str(num_threads), ts_out.name]
        if inject_real_ancestors_from_ts_fn:
            logging.debug("Injecting real ancestors constructed from {}".format(
                inject_real_ancestors_from_ts_fn))
            cmd.extend(["--inject-real-ancestors-from-ts", inject_real_ancestors_from_ts_fn])
        cpu_time, memory_use = time_cmd(cmd)
        ts_simplified = tskit.load(ts_out.name)
    return ts_simplified, cpu_time, memory_use


def run_relate(ts, path_to_vcf, mut_rate, Ne, working_dir, output):
    """
    Run relate software on tree sequence. Requires vcf of simulated data
    Relate needs to run in its own directory (param working_dir) 
    NOTE: Relate's effective population size is "of haplotypes"
    """
    cur_dir = os.getcwd()
    if not os.path.isdir(working_dir):
       os.mkdir(working_dir)
    os.chdir(working_dir)
    subprocess.check_output([os.path.join(cur_dir, relatefileformat_executable),
                             "--mode", "ConvertFromVcf", "--haps",
                             output + ".haps",
                             "--sample", output + ".sample",
                             "-i", path_to_vcf])
    cpu_time, memory_use = time_cmd([os.path.join(cur_dir, relate_executable), "--mode",
                             "All", "-m", str(mut_rate), "-N", str(Ne),
                             "--haps", output + ".haps",
                             "--sample", output + ".sample",
                             "--seed", "1", "-o", output, "--map",
                             os.path.join(cur_dir, "data/genetic_map.txt")])
    subprocess.check_output([os.path.join(cur_dir, relatefileformat_executable), "--mode",
                             "ConvertToTreeSequence",
                             "-i", output, "-o", output])
    relate_ts = tskit.load(output + ".trees")

    # Set samples flags to "1"
    table_collection = relate_ts.dump_tables()
    samples = np.repeat(1, ts.num_samples)
    internal = np.repeat(0, relate_ts.num_nodes - ts.num_samples)
    correct_sample_flags = np.array(
        np.concatenate([samples, internal]), dtype='uint32')
    table_collection.nodes.set_columns(
        flags=correct_sample_flags, time=relate_ts.tables.nodes.time)
    relate_ts_fixed = table_collection.tree_sequence()
    relate_ages = pd.read_csv(output + ".mut", sep=';')
    os.chdir(cur_dir)
    return relate_ts_fixed, relate_ages, cpu_time, memory_use


def run_geva(file_name, Ne, mut_rate, rec_rate):
    """
    Perform GEVA age estimation on a given vcf
    """
    subprocess.check_output([geva_executable, "--out",
                             file_name, "--rec", str(rec_rate),
                             "--vcf", file_name + ".vcf"])
    with open(file_name+".positions.txt", "wb") as out:
        subprocess.call(["awk", "NR>3 {print last} {last = $3}",
                        file_name + ".marker.txt"], stdout=out)
    try:
        cpu_time, memory_use = time_cmd(
            [geva_executable, "-i",
                file_name + ".bin", "--positions",
                file_name + ".positions.txt",
                "--hmm", geva_hmm_initial_probs,
                geva_hmm_emission_probs,
                "--Ne", str(Ne), "--mut", str(mut_rate),
                "--maxConcordant", "200", "--maxDiscordant",
                "200", "-o", file_name + "_estimation"])
    except subprocess.CalledProcessError as grepexc:
        print(grepexc.output)

    age_estimates = pd.read_csv(
        file_name + "_estimation.sites.txt", sep=" ", index_col="MarkerID")
    keep_ages = age_estimates[(age_estimates["Clock"] == "J")
                              & (age_estimates["Filtered"] == 1)]
    return keep_ages, cpu_time, memory_use


def geva_age_estimate(file_name, Ne, mut_rate, rec_rate):
    """
    Perform GEVA age estimation on a given vcf
    """
    file_name = "tmp/" + file_name
    subprocess.check_output([geva_executable, "--out",
                             file_name, "--rec", str(rec_rate),
                             "--vcf", file_name + ".vcf"])
    with open(file_name+".positions.txt", "wb") as out:
        subprocess.call(["awk", "NR>3 {print last} {last = $3}",
                        file_name+".marker.txt"], stdout=out)
    try:
        subprocess.check_output(
            [geva_executable, "-i",
                file_name + ".bin", "--positions",
                file_name + ".positions.txt",
                "--hmm", "/Users/anthonywohns/Documents/mcvean_group/age_inference/"
                "tsdate/tools/geva/hmm/hmm_initial_probs.txt",
                "/Users/anthonywohns/Documents/mcvean_group/age_inference/tsdate/tools/"
                "geva/hmm/hmm_emission_probs.txt",
                "--Ne", str(Ne), "--mut", str(mut_rate),
                "--maxConcordant", "200", "--maxDiscordant",
                "200", "-o", file_name + "_estimation"])
    except subprocess.CalledProcessError as grepexc:
        print(grepexc.output)

    age_estimates = pd.read_csv(
        file_name + "_estimation.sites.txt", sep=" ", index_col="MarkerID")
    keep_ages = age_estimates[(age_estimates["Clock"] == "J")
                              & (age_estimates["Filtered"] == 1)]
    return keep_ages


def run_all_methods_compare(
        index, ts, n, Ne, mutation_rate, recombination_rate, time_grid, grid_slices,
        estimation_method, approximate_prior, error_model, include_geva, seed):
    """
    Function to run all comparisons and return dataframe of mutations
    """
    output = "comparison_" + str(index)
    if error_model is not None:
        error_samples = generate_samples(ts, "error_comparison_" + str(index),
                                         empirical_seq_err_name=error_model)
    # return_vcf(samples, "comparison_" + str(index))
        sampledata_to_vcf(error_samples, "comparison_" + str(index))
    else:
        samples = generate_samples(ts, "comparison_" + str(index))
        sampledata_to_vcf(samples, "comparison_" + str(index))
    dated_ts, inferred_ts, dated_inferred_ts = run_tsdate(
        ts, n, Ne, mutation_rate, time_grid, grid_slices, estimation_method,
        approximate_prior)
    if include_geva:
        geva_ages = geva_age_estimate("comparison_" + str(index),
                                      Ne, mutation_rate, recombination_rate)
        geva_positions = pd.read_csv("tmp/comparison_" + str(index) +
                                     ".marker.txt", delimiter=" ",
                                     index_col="MarkerID")
    relate_output = run_relate(
        ts, "comparison_" + str(index), mutation_rate, Ne * 2, output)
    if include_geva:
        compare_df = compare_mutations(
            ["simulated_ts", "tsdate", "tsdate_inferred", "geva", "relate"],
            [ts, dated_ts, dated_inferred_ts],
            geva_ages=geva_ages, geva_positions=geva_positions,
            relate_ages=relate_output[1])
        tmrca_compare = find_tmrcas_snps({'ts': ts, 'tsdate_true': dated_ts,
                                         'tsdate_inferred': dated_inferred_ts,
                                          'relate': relate_output[0]})
        # kc_distances = [kc_distance_ts(ts, inferred_ts, 0),
        #                 kc_distance_ts(ts, relate_output[0], 0),
        #                 kc_distance_ts(ts, inferred_ts_round2, 0),
        #                 kc_distance_ts(ts, dated_ts, 1),
        #                 kc_distance_ts(ts, dated_inferred_ts, 1),
        #                 kc_distance_ts(ts, tsdated_inferred_ts_wtimes, 1),
        #                 kd_distance_ts(ts, tsdated_ts_round2),
        #                 kc_distance_ts(ts, relate_output[0], 1)]
    else:
        compare_df = compare_mutations(
            ["simulated_ts", "tsdate", "tsdate_inferred", "geva", "relate"],
            [ts, dated_ts, dated_inferred_ts], relate_ages=relate_output[1])
        tmrca_compare = find_tmrcas_snps({'ts': ts, 'tsdate_true': dated_ts,
                                         'tsdate_inferred': dated_inferred_ts,
                                          'relate': relate_output[0]})
        # kc_distances = [kc_distance_ts(ts, inferred_ts, 0),
        #                 kc_distance_ts(ts, relate_output[0], 0),
        #                 kc_distance_ts(ts, inferred_ts_round2, 0),
        #                 kc_distance_ts(ts, dated_ts, 1),
        #                 kc_distance_ts(ts, dated_inferred_ts, 1),
        #                 kc_distance_ts(ts, tsdated_inferred_ts_wtimes, 1),
        #                 kd_distance_ts(ts, tsdated_ts_round2),
        #                 kc_distance_ts(ts, relate_output[0], 1)]

    return compare_df, tmrca_compare


def run_all_tests(params):
    """
    Runs simulation and all tests for the simulation
    """
    index = int(params[0])
    n = int(params[1])
    Ne = float(params[2])
    length = int(params[3])
    mutation_rate = float(params[4])
    recombination_rate = float(params[5])
    model = params[6]
    time_grid = params[7]
    grid_slices = params[8]
    estimation_method = params[9]
    approximate_prior = params[10]
    error_model = params[11]
    include_geva = params[12]
    seed = float(params[13])

    if model == 'neutral':
        ts = run_neutral_sim(
            n, Ne, length, mutation_rate, recombination_rate, seed)
    elif model == 'out_of_africa':
        ts = out_of_africa(n, mutation_rate, recombination_rate, length)
    compare_df, tmrca_compare = run_all_methods_compare(
        index, ts, n, Ne, mutation_rate, recombination_rate, time_grid,
        grid_slices, estimation_method, approximate_prior, error_model, include_geva,
        seed)

    return compare_df, tmrca_compare


def run_multiprocessing(function, params, output, num_replicates, num_processes):
    """
    Run multiprocessing of inputted function a specified number of times
    """
    mutation_results = list()
    tmrca_results = list()
    # kc_distances = list()
    if num_processes > 1:
        logging.info("Setting up using multiprocessing ({} processes)"
                     .format(num_processes))
        with multiprocessing.Pool(processes=num_processes,
                                  maxtasksperchild=2) as pool:
            for result in pool.imap_unordered(function, params):
                #  prior_results = pd.read_csv("data/result")
                #  combined = pd.concat([prior_results, result])
                mutation_results.append(result[0])
                tmrca_results.append(result[1])
                # kc_distances.append(result[2])
    else:
        # When we have only one process it's easier to keep everything in the
        # same process for debugging.
        logging.info("Setting up using a single process")
        for result in map(function, params):
            mutation_results.append(result[0])
            tmrca_results.append(result[1])
            # kc_distances.append(result[2])
    master_mutation_df = pd.concat(mutation_results)
    master_tmrca_df = np.column_stack(tmrca_results)
    master_mutation_df.to_csv("data/" + output + "_mutations")
    np.savetxt("data/" + output + "_tmrcas", master_tmrca_df, delimiter=",")
    # print(kc_distances)
    return master_mutation_df


def plot_results_mutation_compare(result_df, output=None):
    alpha = 0.2
    with sns.axes_style('white'):
        fig, ax = plt.subplots(nrows = 2, ncols = 2, figsize = (12,12), sharex=True, sharey=True)
        true_vals = result_df['simulated_ts']
        tsdate = result_df['tsdate']
        tsdate_inferred = result_df['tsdate_inferred']
        result_df_relate = result_df[result_df['relate'].notnull()]
        relate = result_df_relate['relate']
        true_relate = result_df_relate['simulated_ts']
        
        if 'geva' in result_df.columns:
            result_df_geva = result_df[result_df['geva'].notnull()]
            geva = result_df_geva['geva']
            true_geva = result_df_geva['simulated_ts']
    
        ax[0,0].set_xscale('log')
        ax[0,0].set_yscale('log')
        ax[0,0].set_xlim(1,2e5)
        ax[0,0].set_ylim(1,2e5)

        # tsdate on true tree
        x = true_vals
        y = tsdate 
        ax[0,0].scatter(x, y, s=5, edgecolor='', cmap=plt.cm.viridis, alpha=alpha)
        ax[0,0].plot(ax[0,0].get_xlim(), ax[0,0].get_ylim(), ls="--", c=".3")
        ax[0,0].set_title('tsdate with true topology', fontsize=17)
        ax[0,0].text(0.6, 0.15, "RMSLE:" + "{0:.2f}".format(np.sqrt(mean_squared_log_error(x, y))), fontsize=12, transform=ax[0,0].transAxes)
        ax[0,0].text(0.6, 0.1, "Spearman's "+'$\\rho$: '  + "{0:.2f}".format(scipy.stats.spearmanr(x, y)[0]), fontsize=12, transform=ax[0,0].transAxes)
        ax[0,0].text(0.6, 0.05, "Pearson's r: " + "{0:.2f}".format(scipy.stats.pearsonr(x, y)[0]), fontsize=12, transform=ax[0,0].transAxes)

        # tsdate on inferred tree
        y = tsdate_inferred
        ax[0,1].scatter(x, y, s=5, edgecolor='', cmap=plt.cm.viridis, alpha=alpha)
        ax[0,1].plot(ax[0,0].get_xlim(), ax[0,0].get_ylim(), ls="--", c=".3")
        ax[0,1].set_title('tsdate with inferrred topology', fontsize=17)
        ax[0,1].text(0.6, 0.15, "RMSLE:" + "{0:.2f}".format(np.sqrt(mean_squared_log_error(x, y))), fontsize=12, transform=ax[0,1].transAxes)
        ax[0,1].text(0.6, 0.1, "Spearman's "+'$\\rho$: '  + "{0:.2f}".format(scipy.stats.spearmanr(x, y)[0]), fontsize=12, transform=ax[0,1].transAxes)
        ax[0,1].text(0.6, 0.05, "Pearson's r: " + "{0:.2f}".format(scipy.stats.pearsonr(x, y)[0]), fontsize=12, transform=ax[0,1].transAxes)

        if 'geva' in result_df.columns:
            # geva
            x = true_geva
            y = geva
            xy = np.vstack([x,y])
            z = gaussian_kde(xy)(xy)
            ax[1,0].scatter(x, y, s=5, edgecolor='', cmap=plt.cm.viridis, alpha=alpha)
            ax[1,0].plot(ax[0,0].get_xlim(), ax[0,0].get_ylim(), ls="--", c=".3")
            ax[1,0].set_title('GEVA: Albers & McVean (2018)', fontsize=17)
            ax[1,0].text(0.6, 0.15, "RMSLE:" + "{0:.2f}".format(np.sqrt(mean_squared_log_error(x, y))), fontsize=12, transform=ax[1,0].transAxes)
            ax[1,0].text(0.6, 0.1, "Spearman's "+'$\\rho$: '  + "{0:.2f}".format(scipy.stats.spearmanr(x, y)[0]), fontsize=12, transform=ax[1,0].transAxes)
            ax[1,0].text(0.6, 0.05, "Pearson's r: " + "{0:.2f}".format(scipy.stats.pearsonr(x, y)[0]), fontsize=12, transform=ax[1,0].transAxes)


        # relate
        x = true_relate
        y = relate
        xy = np.vstack([x,y])
        z = gaussian_kde(xy)(xy)
        scatter = ax[1,1].scatter(x, y, s=5, edgecolor='', cmap=plt.cm.viridis, alpha=alpha)
        im = ax[1,1].plot(ax[0,0].get_xlim(), ax[0,0].get_ylim(), ls="--", c=".3")
        ax[1,1].set_title('Relate: Speidel et al. (2019)', fontsize=17)
        ax[1,1].text(0.6, 0.15, "RMSLE:" + "{0:.2f}".format(np.sqrt(mean_squared_log_error(x, y))), fontsize=12, transform=ax[1,1].transAxes)
        ax[1,1].text(0.6, 0.1, "Spearman's "+'$\\rho$: ' + "{0:.2f}".format(scipy.stats.spearmanr(x, y)[0]), fontsize=12, transform=ax[1,1].transAxes)
        ax[1,1].text(0.6, 0.05, "Pearson's r: " + "{0:.2f}".format(scipy.stats.pearsonr(x, y)[0]), fontsize=12, transform=ax[1,1].transAxes)


        fig.text(0.5, 0.04, 'True Mutation Age', size=20, ha='center')
        fig.text(0.04, 0.5, 'Estimated Mutation Age', size=20, va='center', rotation='vertical')

#         cbar_ax = fig.add_axes([0.95, 0.15, 0.05, 0.7])
#         fig.colorbar(scatter, cax=cbar_ax)
        if output:
            plt.savefig(output, dpi=300)


def time_cmd(cmd, stdout=sys.stdout):
    """
    Runs the specified command line (a list suitable for subprocess.call)
    and writes the stdout to the specified file object.
    """
    if sys.platform == 'darwin':
        #on OS X, install gtime using `brew install gnu-time`
        time_cmd = "/usr/local/bin/gtime"
    else:
        time_cmd = "/usr/bin/time"
    full_cmd = [time_cmd, "-f%M %S %U"] + cmd

    with tempfile.TemporaryFile() as stderr:
        exit_status = subprocess.call(full_cmd, stderr=stderr)
        stderr.seek(0)
        if exit_status != 0:
            raise ValueError(
                "Error running '{}': status={}:stderr{}".format(
                    " ".join(cmd), exit_status, stderr.read()))

        split = stderr.readlines()[-1].split()
        # From the time man page:
        # M: Maximum resident set size of the process during its lifetime,
        #    in Kilobytes.
        # S: Total number of CPU-seconds used by the system on behalf of
        #    the process (in kernel mode), in seconds.
        # U: Total number of CPU-seconds that the process used directly
        #    (in user mode), in seconds.
        max_memory = int(split[0]) * 1024
        system_time = float(split[1])
        user_time = float(split[2])
    return user_time + system_time, max_memory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--replicates', type=int,
                        default=10, help="number of replicates")
    parser.add_argument('num_samples', help="number of samples to simulate")
    parser.add_argument('output', help="name of output files")
    parser.add_argument('-n', '--Ne', type=float, default=10000,
                        help="effective population size")
    parser.add_argument("--length", '-l', type=int, default=1e5,
                        help="Length of the sequence")
    parser.add_argument('-m', '--mutation-rate', type=float, default=None,
                        help="mutation rate")
    parser.add_argument('-r', '--recombination-rate', type=float,
                        default=None, help="recombination rate")
    parser.add_argument('--model', type=str, default='neutral',
                        help="choose neutral or out of africa model")
    parser.add_argument('-g', '--grid-slices', type=int, default=50,
                        help="how many slices/quantiles to pass to time grid")
    parser.add_argument('--estimation-method', type=str, default="inside_outside",
                        help="use inside-outside or maximization method")
    parser.add_argument('-e', '--error-model', type=str,
                        default=None, help="input error model")
    parser.add_argument('-t', '--time-grid', type=str, default="adaptive",
                        help="adaptive or uniform time grid")
    parser.add_argument('-a', '--approximate-prior', action="store_true",
                        help="use approximate prior")
    parser.add_argument('--include-geva', action="store_true",
                        help="run comparisons with GEVA")
    parser.add_argument(
        '--seed', '-s', type=int, default=123,
        help="use a non-default RNG seed")
    parser.add_argument(
        "--processes", '-p', type=int, default=1,
        help="number of worker processes, e.g. 40")
    args = parser.parse_args()
    np.random.seed(args.seed)
    rng = random.Random(args.seed)
    seeds = [rng.randint(1, 2**31) for i in range(args.replicates)]
    inputted_params = [int(args.num_samples), args.Ne, args.length,
                       args.mutation_rate, args.recombination_rate, args.model,
                       args.time_grid, args.grid_slices, args.estimation_method,
                       args.approximate_prior, args.error_model, args.include_geva]
    params = iter([np.concatenate([[index], inputted_params, [seed]])
                  for index, seed in enumerate(seeds)])
    mutation_df = run_multiprocessing(run_all_tests, params, args.output, args.replicates,
                        args.processes)
    plot_results_mutation_compare(mutation_df, output="testoutput")

if __name__ == "__main__":
    main()
