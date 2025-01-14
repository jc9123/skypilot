"""A script that queries Azure API to get instance types and pricing info.

This script takes about 1 minute to finish.
"""
import argparse
import json
from multiprocessing import pool as mp_pool
import os
import subprocess
from typing import List, Optional, Set
import urllib

import numpy as np
import pandas as pd
import requests

US_REGIONS = [
    'centralus',
    'eastus',
    'eastus2',
    'northcentralus',
    'southcentralus',
    'westcentralus',
    'westus',
    'westus2',
    'westus3',
]

# Exclude the following regions as they do not have ProductName in the
# pricing table. Reference: #1768 #2548
EXCLUDED_REGIONS = {
    'eastus2euap',
    'centraluseuap',
    'brazilus',
}

SINGLE_THREADED = False


def get_regions() -> List[str]:
    """Get all available regions."""
    proc = subprocess.run(
        'az account list-locations  --query "[?not_null(metadata.latitude)] '
        '.{RegionName:name , RegionDisplayName:regionalDisplayName}" -o json',
        shell=True,
        check=True,
        stdout=subprocess.PIPE)
    items = json.loads(proc.stdout.decode('utf-8'))
    regions = [
        item['RegionName']
        for item in items
        if not item['RegionName'].endswith('stg')
    ]
    return regions


# Azure secretly deprecated the M60 family which is still returned by its API.
# We have to manually remove it.
DEPRECATED_FAMILIES = ['standardNVSv2Family']

USEFUL_COLUMNS = [
    'InstanceType', 'AcceleratorName', 'AcceleratorCount', 'vCPUs', 'MemoryGiB',
    'GpuInfo', 'Price', 'SpotPrice', 'Region', 'Generation', 'DeviceMemory'
]


def get_pricing_url(region: Optional[str] = None) -> str:
    filters = [
        'serviceName eq \'Virtual Machines\'',
        'priceType eq \'Consumption\'',
    ]
    if region is not None:
        filters.append(f'armRegionName eq \'{region}\'')
    filters_str = urllib.parse.quote(' and '.join(filters))
    return f'https://prices.azure.com/api/retail/prices?$filter={filters_str}'


def get_pricing_df(region: Optional[str] = None) -> pd.DataFrame:
    all_items = []
    url = get_pricing_url(region)
    print(f'Getting pricing for {region}')
    page = 0
    while url is not None:
        page += 1
        if page % 10 == 0:
            print(f'Fetched pricing pages {page}')
        r = requests.get(url)
        r.raise_for_status()
        content_str = r.content.decode('ascii')
        content = json.loads(content_str)
        items = content.get('Items', [])
        if len(items) == 0:
            break
        all_items += items
        url = content.get('NextPageLink')
    print(f'Done fetching pricing {region}')
    df = pd.DataFrame(all_items)
    assert 'productName' in df.columns, (region, df.columns)
    return df[(~df['productName'].str.contains(' Windows')) &
              (df['unitPrice'] > 0)]


def get_sku_df(region_set: Set[str]) -> pd.DataFrame:
    print('Fetching SKU list')
    # To get a complete list, --all option is necessary.
    proc = subprocess.run(
        'az vm list-skus --all --resource-type virtualMachines -o json',
        shell=True,
        check=True,
        stdout=subprocess.PIPE,
    )
    print('Done fetching SKUs')
    items = json.loads(proc.stdout.decode('ascii'))
    filtered_items = []
    for item in items:
        # zones = item['locationInfo'][0]['zones']
        region = item['locations'][0]
        if region.lower() not in region_set:
            continue
        item['Region'] = region
        filtered_items.append(item)

    df = pd.DataFrame(filtered_items)
    return df


def get_gpu_name(family: str) -> Optional[str]:
    gpu_data = {
        'standardNCFamily': 'K80',
        'standardNCSv2Family': 'P100',
        'standardNCSv3Family': 'V100',
        'standardNCPromoFamily': 'K80',
        'StandardNCASv3_T4Family': 'T4',
        'standardNDSv2Family': 'V100-32GB',
        'StandardNCADSA100v4Family': 'A100-80GB',
        'standardNDAMSv4_A100Family': 'A100-80GB',
        'StandardNDASv4_A100Family': 'A100',
        'standardNVFamily': 'M60',
        'standardNVSv2Family': 'M60',
        'standardNVSv3Family': 'M60',
        'standardNVPromoFamily': 'M60',
        'standardNVSv4Family': 'Radeon MI25',
        'standardNDSFamily': 'P40',
        'StandardNVADSA10v5Family': 'A10',
    }
    # NP-series offer Xilinx U250 FPGAs which are not GPUs,
    # so we do not include them here.
    # https://docs.microsoft.com/en-us/azure/virtual-machines/np-series
    family = family.replace(' ', '')
    return gpu_data.get(family)


def get_all_regions_instance_types_df(region_set: Set[str]):
    if SINGLE_THREADED:
        dfs = [get_pricing_df(region) for region in region_set]
        df_sku = get_sku_df(region_set)
        df = pd.concat(dfs)
    else:
        with mp_pool.Pool() as pool:
            dfs_result = pool.map_async(get_pricing_df, region_set)
            df_sku_result = pool.apply_async(get_sku_df, (region_set,))

            dfs = dfs_result.get()
            df_sku = df_sku_result.get()
            df = pd.concat(dfs)

    print('Processing dataframes')
    df.drop_duplicates(inplace=True)

    df = df[df['unitPrice'] > 0]

    print('Getting price df')
    df['merge_name'] = df['armSkuName']
    # Use lower case for the Region, as for westus3, the SKU API returns
    # WestUS3.
    # This is inconsistent with the region name used in the pricing API, and
    # the case does not matter for launching instances, so we can safely
    # discard the case.
    df['Region'] = df['armRegionName'].str.lower()
    df['is_promo'] = df['skuName'].str.endswith(' Low Priority')
    df.rename(columns={
        'armSkuName': 'InstanceType',
    }, inplace=True)
    demand_df = df[~df['skuName'].str.contains(' Spot')][[
        'is_promo', 'InstanceType', 'Region', 'unitPrice'
    ]]
    spot_df = df[df['skuName'].str.contains(' Spot')][[
        'is_promo', 'InstanceType', 'Region', 'unitPrice'
    ]]

    demand_df.set_index(['InstanceType', 'Region', 'is_promo'], inplace=True)
    spot_df.set_index(['InstanceType', 'Region', 'is_promo'], inplace=True)

    demand_df = demand_df.rename(columns={'unitPrice': 'Price'})
    spot_df = spot_df.rename(columns={'unitPrice': 'SpotPrice'})

    print('Getting sku df')
    df_sku['is_promo'] = df_sku['name'].str.endswith('_Promo')
    df_sku.rename(columns={'name': 'InstanceType'}, inplace=True)

    df_sku['merge_name'] = df_sku['InstanceType'].str.replace('_Promo', '')
    df_sku['Region'] = df_sku['Region'].str.lower()

    print('Joining')
    df = df_sku.join(demand_df,
                     on=['merge_name', 'Region', 'is_promo'],
                     how='left')
    df = df.join(spot_df, on=['merge_name', 'Region', 'is_promo'], how='left')

    def get_capabilities(row):
        gpu_name = None
        gpu_count = np.nan
        vcpus = np.nan
        memory = np.nan
        gen_version = None
        caps = row['capabilities']
        for item in caps:
            assert isinstance(item, dict), (item, caps)
            if item['name'] == 'GPUs':
                gpu_name = get_gpu_name(row['family'])
                if gpu_name is not None:
                    gpu_count = item['value']
            elif item['name'] == 'vCPUs':
                vcpus = float(item['value'])
            elif item['name'] == 'MemoryGB':
                memory = item['value']
            elif item['name'] == 'HyperVGenerations':
                gen_version = item['value']
        return gpu_name, gpu_count, vcpus, memory, gen_version

    def get_additional_columns(row):
        gpu_name, gpu_count, vcpus, memory, gen_version = get_capabilities(row)
        return pd.Series({
            'AcceleratorName': gpu_name,
            'AcceleratorCount': gpu_count,
            'vCPUs': vcpus,
            'MemoryGiB': memory,
            'GpuInfo': gpu_name,
            'Generation': gen_version,
        })

    df_ret = pd.concat(
        [df, df.apply(get_additional_columns, axis='columns')],
        axis='columns',
    )

    def create_gpu_map(df):
        # Map of Azure's machine with GPU to their corresponding memory
        # Result is hard-coded since Azure's API to not return such info
        # may be outdated so need to be maintained
        gpu_map = {
            'Standard_NC6': 12,
            'Standard_NC12': 24,
            'Standard_NC24': 48,
            'Standard_NC24r*': 48,
            'Standard_NC6s_v2': 16,
            'Standard_NC12s_v2': 32,
            'Standard_NC24s_v2': 64,
            'Standard_NC24rs_v2*': 64,
            'Standard_NC6s_v3': 16,
            'Standard_NC12s_v3': 32,
            'Standard_NC24s_v3': 32,
            'Standard_NC4as_T4_v3': 16,
            'Standard_NC8as_T4_v3': 16,
            'Standard_NC16as_T4_v3': 16,
            'Standard_NC64as_T4_v3': 64,
            'Standard_NC24ads_A100_v4': 80,
            'Standard_NC48ads_A100_v4': 160,
            'Standard_NC96ads_A100_v4': 320,
            'Standard_ND96asr_v4': 40,
            'Standard_ND96amsr_A100_v4': 80,
            'Standard_ND6s': 24,
            'Standard_ND12s': 48,
            'Standard_ND24s': 96,
            'Standard_ND24rs*': 96,
            'Standard_ND40rs_v2': 32,
            'Standard_NG8ads_V620_v1': 8,
            'Standard_NG16ads_V620_v1': 16,
            'Standard_NG32ads_V620_v1': 32,
            'Standard_NG32adms_V620_v1': 32,
            'Standard_NV6': 8,
            'Standard_NV12': 16,
            'Standard_NV24': 32,
            'Standard_NV12s_v3': 8,
            'Standard_NV24s_v3': 16,
            'Standard_NV48s_v3': 32,
            'Standard_NV4as_v4': 2,
            'Standard_NV8as_v4': 4,
            'Standard_NV16as_v4': 8,
            'Standard_NV32as_v4': 16,
            'Standard_NV6ads_A10_v5': 4,
            'Standard_NV12ads_A10_v5': 8,
            'Standard_NV18ads_A10_v5': 12,
            'Standard_NV36ads_A10_v5': 24,
            'Standard_NV36adms_A10_v5': 24,
            'Standard_NV72ads_A10_v5': 48,
            'Standard_NV6_Promo': 16,
            'Standard_NV12_Promo': 32,
            'Standard_NV24_Promo': 48
        }

        all_instance = df.InstanceType.unique()

        for instance in all_instance:
            if instance not in gpu_map:
                gpu_map[instance] = ''
        return gpu_map

    def map_device_memory(row, dic):
        return dic[row]

    before_drop_len = len(df_ret)
    df_ret.dropna(subset=['InstanceType'], inplace=True, how='all')
    after_drop_len = len(df_ret)
    print(f'Dropped {before_drop_len - after_drop_len} duplicated rows')

    df_ret['DeviceMemory'] = df_ret.InstanceType.apply(
        map_device_memory, args=(create_gpu_map(df_ret),))

    # Filter out deprecated families
    df_ret = df_ret.loc[~df_ret['family'].isin(DEPRECATED_FAMILIES)]
    df_ret = df_ret[USEFUL_COLUMNS]
    return df_ret


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
<<<<<<< HEAD
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--all-regions',
                       action='store_true',
                       help='Fetch all global regions, not just the U.S. ones.')
    group.add_argument('--regions',
                       nargs='+',
                       help='Fetch the list of specified regions.')
    parser.add_argument('--exclude',
                        nargs='+',
                        help='Exclude the list of specified regions.')
    parser.add_argument('--single-threaded',
                        action='store_true',
                        help='Run in single-threaded mode. This is useful when '
                        'running in github action, as the multiprocessing '
                        'does not work well with the azure client due '
                        'to ssl issues.')
    args = parser.parse_args()

    SINGLE_THREADED = args.single_threaded

    if args.regions:
        region_filter = set(args.regions) - EXCLUDED_REGIONS
    elif args.all_regions:
        region_filter = set(get_regions()) - EXCLUDED_REGIONS
    else:
        region_filter = US_REGIONS
    region_filter = region_filter - set(
        args.exclude) if args.exclude else region_filter

    if not region_filter:
        raise ValueError('No regions to fetch. Please check your arguments.')
=======
    parser.add_argument(
        '--all-regions',
        action='store_true',
        help='Fetch all global regions, not just the U.S. ones.')
    args = parser.parse_args()

    region_filter = get_regions() if args.all_regions else US_REGIONS
    region_filter = set(region_filter) - EXCLUDED_REGIONS
>>>>>>> AddDeviceMemory

    instance_df = get_all_regions_instance_types_df(region_filter)
    os.makedirs('azure', exist_ok=True)
    instance_df.to_csv('azure/vms.csv', index=False)
    print('Azure Service Catalog saved to azure/vms.csv')
