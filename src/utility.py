"""
Useful functions used in multiple scripts.
"""

import numpy as np
import pandas as pd

import tskit


def get_mut_ages(ts, dates):
    mut_ages = list()
    mut_upper_bounds = list()
    for tree in ts.trees():
        for site in tree.sites():
            for mut in site.mutations:
                parent_age = dates[tree.parent(mut.node)]
                mut_upper_bounds.append(parent_age)
                mut_ages.append((dates[mut.node] + parent_age) / 2)
    return np.array(mut_ages), np.array(mut_upper_bounds)


def get_mut_ages_dict(ts, dates, exclude_root=False):
    mut_ages = dict()
    for tree in ts.trees():
        for site in tree.sites():
            for mut in site.mutations:
                parent_node = tree.parent(mut.node)
                if exclude_root and parent_node == ts.num_nodes - 1:
                    continue
                else:
                    parent_age = dates[parent_node]
                    mut_ages[site.position] = ((dates[mut.node] + parent_age) / 2)
    return mut_ages

def get_mut_pos_df(ts, name, node_dates, exclude_root=False):
    mut_dict = get_mut_ages_dict(ts, node_dates, exclude_root=exclude_root) 
    mut_df = pd.DataFrame.from_dict(mut_dict, orient="index", columns=[name])
    mut_df.index = np.round(mut_df.index)
    mut_df = mut_df.loc[~mut_df.index.duplicated()]
    return mut_df

