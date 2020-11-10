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
    site_ages = dict()
    for tree in ts.trees():
        for site in tree.sites():
            site_ages[site.position] = -np.inf
            for mut in site.mutations:
                parent_node = tree.parent(mut.node)
                if exclude_root and parent_node == ts.num_nodes - 1:
                    continue
                else:
                    parent_age = dates[parent_node]
                    mut_age = np.sqrt(dates[mut.node] * parent_age)
                    if site_ages[site.position] < mut_age:
                        site_ages[site.position] = mut_age
    return site_ages 

def get_mut_pos_df(ts, name, node_dates, exclude_root=False):
    mut_dict = get_mut_ages_dict(ts, node_dates, exclude_root=exclude_root) 
    mut_df = pd.DataFrame.from_dict(mut_dict, orient="index", columns=[name])
    mut_df.index = (np.round(mut_df.index)).astype(int)
    sort_dates = mut_df.sort_values(by=[name], ascending=False, kind="mergesort")
    mut_df = sort_dates.groupby(sort_dates.index).first()
#    mut_df = mut_df.loc[~mut_df.index.duplicated()]
    return mut_df

def weighted_geographic_center(lat_list, long_list, weights):
    x = list()
    y = list()
    z = list()
    if len(lat_list) == 1 and len(long_list) == 1:
        return(lat_list[0], long_list[0])
    lat_radians = np.radians(lat_list)
    long_radians = np.radians(long_list)
    x = (np.cos(lat_radians) * np.cos(long_radians))
    y = (np.cos(lat_radians) * np.sin(long_radians))
    z = (np.sin(lat_radians))
    
    weights = np.array(weights)
    central_latitude, central_longitude = radians_center_weighted(x, y, z, weights)

    return(np.degrees(central_latitude), np.degrees(central_longitude))

def radians_center_weighted(x, y, z, weights):
    total_weight = np.sum(weights)
    weighted_avg_x = np.sum(weights * np.array(x)) / total_weight
    weighted_avg_y = np.sum(weights * np.array(y)) / total_weight
    weighted_avg_z = np.sum(weights * np.array(z)) / total_weight
    central_longitude = np.arctan2(weighted_avg_y, weighted_avg_x)
    central_square_root = np.sqrt(
            weighted_avg_x * weighted_avg_x + weighted_avg_y * weighted_avg_y)
    central_latitude = np.arctan2(weighted_avg_z, central_square_root)
    return central_latitude, central_longitude
