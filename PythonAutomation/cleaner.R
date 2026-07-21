# Modified R processing script based on original implementation by Kat Tisshaw
# Modified to add conditonal phase implementation and automatic file selection, also now expects tab delimited files instead of csv files and automatically selects the correct files without asking for user input
# section/line modifications are noted with comments in the format of "### KDMOD"

# NOTE: I had noted at some point that this script contains a math error somewhere, I forget where but I remembered last night before falling asleep and thought that I should note it here so that I can fix it later. I think it has to do with the filtering of the decimalLatitude and decimalLongitude columns to only include those with 3 decimal places, but I'm not sure and I'm also not sure why I didn't fix it when I spotted it.

### relax on geolocation filtering restrictions, but keep the other filters in place. This is because some of the occurrences are from the 1800s and 1900s and the geolocation data is not as precise as it is today. The filtering of the decimalLatitude and decimalLongitude columns to only include those with 3 decimal places is too strict and removes too many occurrences that are still valid.

args <- commandArgs(trailingOnly = TRUE) ### KDMOD

# Check if parameters were provided ### KDMOD
if (length(args) < 4) { 
  stop("Error: This script requires at least four arguments.")
}

library(tidyverse)
library(CoordinateCleaner)

### KDMOD
phase <- args[1]
occurrence_file <- args[2]
multimedia_file <- args[3]
output_file <- args[4]

if (phase == "1") { ### KDMOD

  print("Phase 1: Initial data cleaning and merging.") ### KDMOD

  gbif_db = read.delim(occurrence_file) #choose the "01-06-2025_occurrence.csv" file ### KDMOD
  media_db = read.delim(multimedia_file) #choose the "01-06-2025_multimedia.csv" file ### KDMOD

  # within the multimedia spreadsheet, you need to first update the names of columns with "_multimedia" because these column names are
  #exactly the same in the occurrence spreadsheet, but they have different contents.
  media_db <- rename(media_db, type_multimedia = type, references_multimedia = references, publisher_multimedia = publisher,
                    license_multimedia = license, rightsHolder_multimedia = rightsHolder)

  #remove any columns that only have NAs (this is only to make the spreadsheet easier to parse through)
  gbif_db <- gbif_db %>%
    select(where(~ !all(is.na(.))))

  #join multimedia spreadsheet to gbif occurrence
  master_df <- media_db %>%
    full_join(gbif_db, by = c("gbifID"))

  #filtering the spreadsheet to only have latitudes and longitudes with 3 decimal places:
  master_df <- master_df[grep("\\.[1-9][1-9][1-9]", master_df$decimalLatitude), ]
  master_df <- master_df[grep("\\.[1-9][1-9][1-9]", master_df$decimalLongitude), ]

  #Check all entries in the scientific names and infraspecific epithet columns. Please note that sometimes the variety is in scientificName but
  #not infraspecific epithet, and vice versa. This means that if you're concerned with subspecies, you'll really need to observe the entries in
  #both columns and be careful that any aren't kept or removed erroneously.
  unique(gbif_db$scientificName)
  unique(gbif_db$infraspecificEpithet)

  #removed occurrences with the following scientificName:
  master_df <- master_df %>%
    filter(!scientificName %in% c("Megathyrsus maximus var. coloratus (C.T.White) B.K.Simon & S.W.L.Jacobs",
                                  "Megathyrsus maximus var. pubiglumis (K.Schum.) B.K.Simon & S.W.L.Jacobs",
                                  "Panicum compressum Biv.",
                                  "Panicum trichoglume K.Schum.",
                                  "Panicum maximum var. effusum A.Camus",
                                  "Panicum mahafalense A.Camus",
                                  "Panicum maximum var. pubiglume K.Schum",
                                  "Panicum maximum var. trichoglume Robyns"))

  #Adjust this code acccording to available infraspecific epithets we don't want
  master_df <- master_df %>%
    filter(!infraspecificEpithet %in% c("Megathyrsus maximus var. coloratus (C.T.White) B.K.Simon & S.W.L.Jacobs",
                                        "Megathyrsus maximus var. pubiglumis (K.Schum.) B.K.Simon & S.W.L.Jacobs",
                                        "Panicum compressum Biv.",
                                        "Panicum trichoglume K.Schum.",
                                        "Panicum maximum var. effusum A.Camus",
                                        "Panicum mahafalense A.Camus",
                                        "Panicum maximum var. pubiglume K.Schum",
                                        "Panicum maximum var. trichoglume Robyns"))
  #Adjust this code acccording as well
  master_df <- master_df %>%
    filter(!verbatimScientificName %in% c("Megathyrsus maximus var. coloratus (C.T.White) B.K.Simon & S.W.L.Jacobs",
                                          "Megathyrsus maximus var. pubiglumis (K.Schum.) B.K.Simon & S.W.L.Jacobs",
                                          "Panicum compressum Biv.",
                                          "Panicum trichoglume K.Schum.",
                                          "Panicum maximum var. effusum A.Camus",
                                          "Panicum mahafalense A.Camus",
                                          "Panicum maximum var. pubiglume K.Schum",
                                          "Panicum maximum var. trichoglume Robyns"))

  #clean data using the wrapper function for all three tests
  master_df <- clean_coordinates(
    x = master_df,
    lon = "decimalLongitude",
    lat = "decimalLatitude",
    species = "species", #species column is needed for some functions - not sure if it's needed for mine here but added anyways
    tests = c("centroids", "seas","institutions"),
    value = "clean") #returns a data frame with problematic records removed

  unique(master_df$issue)

  #these are all related to unreliable geospatial data:
  bad_issues <- c("COORDINATE_ROUNDED",
                  "GEODETIC_DATUM_INVALID",
                  "GEODETIC_DATUM_ASSUMED_WGS84",
                  "COORDINATE_PRECISION_INVALID",
                  "COORDINATE_UNCERTAINTY_METERS_INVALID",
                  "FOOTPRINT_INVALID",
                  "FOOTPRINT_WKT_MISMATCH",
                  "CONTINENT_COORDINATE_MISMATCH",
                  "COUNTRY_COORDINATE_MISMATCH",
                  "CONTINENT_COUNTRY_MISMATCH")

  #removes any occurrences with the unreliable geospatial data "bad_issues":
  master_df <- master_df %>%
    filter(is.na(issue) | !str_detect(issue, str_c(bad_issues, collapse = "|")))

  unique(master_df$issue)

  inspect_issues <- c("COUNTRY_MISMATCH",
                      "RECORDED_DATE_MISMATCH",
                      "RECORDED_DATE_INVALID",
                      "RECORDED_DATE_UNLIKELY",
                      "OCCURRENCE_STATUS_UNPARSABLE")

  master_df <- master_df %>%
    mutate(inspect_flag = if_else(!is.na(issue) &
                                    str_detect(issue, str_c(inspect_issues, collapse = "|")), TRUE, FALSE))
  #Export for manual inspection:
  write.csv(master_df,"GBIFdownload_inspectFlags.csv") #Prior to inspecting rows, sort by Coordinate Uncertainty and
  #remove any occurrences over 5000m UNLESS precision is specified (and is a very small number). Create a notes column and
  #enter 'Remove' in rows where there are suspicious collections like "cultivated" or "botanic gardens" in the locality
  #column. Check other columns by using the Excel Data filter to get a list to scroll through (as I've noticed with other
  #GBIF data, data entries aren't always correct and notes are put in the wrong columns). Finally, inspect the occurrences
  #that are marked as TRUE in the inspect_flag column, add 'Remove' to notes if anything warrants removing.


} else if (phase == "2") { ### KDMOD
 
  print("Phase 2: Further data cleaning and inspection.") ### KDMOD

  #reload spreadsheet:
  master_df = read.csv(output_file, na.strings = "") #choose the updated "GBIFdownload_inspectFlags.csv" file ### KDMOD

  #create Action column if it doesn't exist (for first-time users with no manual edits)
  if (!"Action" %in% names(master_df)) {
    master_df$Action <- NA
  }

  #remove any columns flagged as "Remove"
  master_df <- master_df %>%
    filter(is.na(Action) | Action != "Remove")

  master_df_ND <- master_df %>%   #ND for no duplicates
    distinct(decimalLatitude, decimalLongitude, .keep_all = TRUE)

  removed_coords <- master_df %>%
    group_by(decimalLatitude, decimalLongitude) %>%
    filter(n() > 1) %>%      #take only duplicated coordinate groups
    slice(-1) %>%            #remove the first of a set that is duplicated from this df
    ungroup()

  #code to remove any columns that only have NAs:
  removed_coords <- removed_coords %>%
    select(where(~ !all(is.na(.))))

  write.csv(removed_coords,"01-06-2025_DuplicateRecordsRemovedfromMaster.csv")

  write.csv(master_df_ND,"01-06-2025_master.csv")

} else { ### KDMOD
  stop("Error: Invalid phase specified. Use '1' or '2'.")
}

#exported, changed to .xls file, and did further cleaning which required inspecting spreadsheet and removing occurrences that did not have a
#valid entry for ANY of the following columns:
# “identifier”
# “references[_multimedia]”
# “bibliographicCitation”
# “references”
# “associatedReferences”
# "occurrenceID"

