import subprocess, os
from unittest import case
from PythonAutomation import trigger_download, wait_and_download, extract_archive, clean_and_prepare_data_with_R, finalize_dataset, resolve_species
# clean_and_prepare_data

# Change this target species for new projects. 
TARGET_SPECIES = "Megathyrsus maximus"

VARIETIES_TO_DROP = [
    "Megathyrsus maximus var. coloratus (C.T.White) B.K.Simon & S.W.L.Jacobs",
    "Megathyrsus maximus var. pubiglumis (K.Schum.) B.K.Simon & S.W.L.Jacobs",
    "Panicum compressum Biv.",
    "Panicum trichoglume K.Schum.",
    "Panicum maximum var. effusum A.Camus",
    "Panicum mahafalense A.Camus",
    "Panicum maximum var. pubiglume K.Schum",
    "Panicum maximum var. trichoglume Robyns"
]

DOWNLOAD_FORMAT = "DWCA"  # Options: "DWCA" or "SIMPLE_CSV"

DATA_PREPARATION_VERSION = 0 #Options: 0 for original R script, 1 for new python script

GLOBAL_DL_KEY = None  # Global variable to store the download key
GLOBAL_EXTRACT_FOLDER = None  # Global variable to store the extraction folder path

# GBIF Account Details
GBIF_USER = "bfl_ut_austin" 
GBIF_PASSWORD = "qwertyuiop123"
GBIF_EMAIL = "kaustubhduddala@utexas.edu"

if __name__ == "__main__":
    def Download(info_only=False):
        print(f"starting download for {TARGET_SPECIES}")
        
        taxon_key = resolve_species(TARGET_SPECIES)
        
        # NOTE: Futhur filtering is done in the clean_and_prepare_data function, so we only need to filter for PRESERVED_SPECIMEN, hasCoordinate, and occurrenceStatus=PRESENT here.
        queries = [
            f"taxonKey = {taxon_key}",
            "basisOfRecord = PRESERVED_SPECIMEN",
            "hasCoordinate = True",
            "occurrenceStatus = PRESENT",
        ]

        dl_key = trigger_download(queries, DOWNLOAD_FORMAT, GBIF_USER, GBIF_PASSWORD, GBIF_EMAIL)
        GLOBAL_DL_KEY = dl_key

        if not info_only:
            zip_file = wait_and_download(dl_key)
            extract_folder = extract_archive(zip_file, f"./data_{dl_key}")
            GLOBAL_EXTRACT_FOLDER = extract_folder
        else:
            return dl_key  # Return the download key for info_only mode

    def Prepare(phase, dl_key=GLOBAL_DL_KEY, extract_folder=GLOBAL_EXTRACT_FOLDER):

        if DATA_PREPARATION_VERSION == 0:
            clean_and_prepare_data_with_R(phase, f"./data_{dl_key}/occurrence.txt", f"./data_{dl_key}/multimedia.txt", output_csv="GBIFdownload_inspectFlags.csv")
        elif DATA_PREPARATION_VERSION == 1:
            clean_and_prepare_data(extract_folder, output_csv="GBIFdownload_inspectFlags.csv", exclude_taxa=VARIETIES_TO_DROP)
            finalize_dataset(extract_folder, output_csv="GBIFdownload_inspectFlags.csv", exclude_taxa=VARIETIES_TO_DROP)
        else:
            raise ValueError("Invalid DATA_PREPARATION_VERSION. Must be 0 or 1.")
    
    choice = input("Select an option: \n1: Download and Prepare Data \n2: Download Data Only \n3: Prepare Data Only \nEnter your choice (1/2/3): ")

    match choice:
        case "1":
            Download()
            Prepare(phase=1, dl_key=Download(info_only=True))
            input("Press Enter to continue after manual review...")
            Prepare(phase=2, dl_key=Download(info_only=True))
        case "2":
            Download()
        case "3":
            choice2 = input("enter .zip folder name or click enter to use the latest download matching configuration: ")
            match choice2:
                case "":
                    dl_key = Download(info_only=True)
                case _:
                    dl_key = choice2 if choice2 else Download(info_only=True)
                    GLOBAL_DL_KEY = dl_key
            Prepare(phase=1, dl_key=dl_key)
            input("Press Enter to continue after manual review...")
            Prepare(phase=2, dl_key=dl_key)
        case _:
            print("Unknown command.")