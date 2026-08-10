"""
GBIF Herbaria Data Pipeline - Combined GUI + CLI
=====================

Choose between graphical interface or command line.

Usage:
    python main.py
    
Then select:
    1 - GUI (Graphical interface with tkinter)
    2 - CLI (Command line interface)
"""

import os
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk, filedialog, messagebox, scrolledtext

from PythonAutomation.prepare_data import resolve_species, trigger_download, wait_and_download, extract_archive
from PythonAutomation.cleaner import phase_1_clean_and_merge, phase_2_finalize_dataset


# Configuration
GBIF_USER = "bfl_ut_austin"
GBIF_PASSWORD = "qwertyuiop123"
GBIF_EMAIL = "kaustubhduddala@utexas.edu"

EXCLUDE_TAXA = [
    "Megathyrsus maximus var. coloratus (C.T.White) B.K.Simon & S.W.L.Jacobs",
    "Megathyrsus maximus var. pubiglumis (K.Schum.) B.K.Simon & S.W.L.Jacobs",
    "Panicum compressum Biv.",
    "Panicum trichoglume K.Schum.",
    "Panicum maximum var. effusum A.Camus",
    "Panicum mahafalense A.Camus",
    "Panicum maximum var. pubiglume K.Schum",
    "Panicum maximum var. trichoglume Robyns"
]

# Presets for GUI
PRESETS = {
    "G064 (Default)": {
        "species": "Megathyrsus maximus",
        "relax_precision": True,
        "exclude_taxa": [
            "Megathyrsus maximus var. coloratus (C.T.White) B.K.Simon & S.W.L.Jacobs",
            "Megathyrsus maximus var. pubiglumis (K.Schum.) B.K.Simon & S.W.L.Jacobs",
            "Panicum compressum Biv.",
            "Panicum trichoglume K.Schum.",
            "Panicum maximum var. effusum A.Camus",
            "Panicum mahafalense A.Camus",
            "Panicum maximum var. pubiglume K.Schum",
            "Panicum maximum var. trichoglume Robyns"
        ],
    },
    "Custom": {
        "species": "",
        "relax_precision": True,
        "exclude_taxa": [],
    }
}


# ==============
# GUI APPLICATION (TKINTER)
# ==============

class GBIFPipelineGUI:
    """Simple tkinter-based GUI for GBIF pipeline."""
    
    def __init__(self, root):
        self.root = root
        self.root.title("GBIF Herbaria Data Pipeline")
        self.root.geometry("700x800")
        
        self.worker_thread = None
        self.is_running = False
        
        self.setup_ui()
    
    def setup_ui(self):
        """Setup the user interface."""
        # Main frame
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.grid(row=0, column=0, sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        
        # Title
        title = ttk.Label(main_frame, text="GBIF Herbaria Data Pipeline", 
                         font=("Arial", 14, "bold"))
        title.grid(row=0, column=0, columnspan=3, pady=(0, 15))
        
        # Workflow selection
        workflow_frame = ttk.LabelFrame(main_frame, text="Workflow", padding="10")
        workflow_frame.grid(row=1, column=0, columnspan=3, sticky="ew", pady=10)
        
        self.workflow_var = tk.StringVar(value="full")
        ttk.Radiobutton(workflow_frame, text="Download Only", variable=self.workflow_var, 
                       value="download", command=self.update_ui).pack(anchor="w")
        ttk.Radiobutton(workflow_frame, text="Prepare Only (Phase 1 & 2)", variable=self.workflow_var,
                       value="prepare", command=self.update_ui).pack(anchor="w")
        ttk.Radiobutton(workflow_frame, text="Full Workflow (Download + Prepare)", variable=self.workflow_var,
                       value="full", command=self.update_ui).pack(anchor="w")
        
        # Species input (shown for download/full)
        self.species_frame = ttk.LabelFrame(main_frame, text="Species Selection", padding="10")
        self.species_frame.grid(row=2, column=0, columnspan=3, sticky="ew", pady=10)
        
        ttk.Label(self.species_frame, text="Species Name:").pack(anchor="w")
        self.species_entry = ttk.Entry(self.species_frame, width=50)
        self.species_entry.insert(0, "Megathyrsus maximus")
        self.species_entry.pack(fill="x", pady=5)
        
        # Data selection (shown for prepare)
        self.data_frame = ttk.LabelFrame(main_frame, text="Data Selection", padding="10")
        
        ttk.Label(self.data_frame, text="Data Folder or ZIP:").pack(anchor="w")
        self.data_entry = ttk.Entry(self.data_frame, width=50)
        self.data_entry.pack(fill="x", pady=5)
        
        ttk.Button(self.data_frame, text="Browse...", 
                  command=self.browse_data).pack(anchor="e", pady=5)
        
        # Phase selection (shown for prepare)
        self.phase_frame = ttk.LabelFrame(main_frame, text="Phases", padding="10")
        
        self.phase_var = tk.StringVar(value="both")
        ttk.Radiobutton(self.phase_frame, text="Phase 1 & 2", variable=self.phase_var,
                       value="both").pack(anchor="w")
        ttk.Radiobutton(self.phase_frame, text="Phase 1 Only", variable=self.phase_var,
                       value="phase1").pack(anchor="w")
        ttk.Radiobutton(self.phase_frame, text="Phase 2 Only", variable=self.phase_var,
                       value="phase2").pack(anchor="w")
        
        # Filters
        filters_frame = ttk.LabelFrame(main_frame, text="Filters", padding="10")
        filters_frame.grid(row=3, column=0, columnspan=3, sticky="ew", pady=10)
        
        ttk.Label(filters_frame, text="Preset:").pack(anchor="w")
        self.preset_var = tk.StringVar(value="G064 (Default)")
        preset_combo = ttk.Combobox(filters_frame, textvariable=self.preset_var,
                                    values=list(PRESETS.keys()), state="readonly")
        preset_combo.pack(fill="x", pady=5)
        preset_combo.bind("<<ComboboxSelected>>", lambda e: self.load_preset())
        
        ttk.Label(filters_frame, text="Coordinate Precision:").pack(anchor="w", pady=(10, 0))
        self.precision_var = tk.StringVar(value="relaxed")
        ttk.Radiobutton(filters_frame, text="Relaxed (≥1 decimal place)", 
                       variable=self.precision_var, value="relaxed").pack(anchor="w")
        ttk.Radiobutton(filters_frame, text="Strict (≥3 decimal places)",
                       variable=self.precision_var, value="strict").pack(anchor="w")
        
        ttk.Label(filters_frame, text="Taxa Exclusions (one per line):").pack(anchor="w", pady=(10, 0))
        self.taxa_text = tk.Text(filters_frame, height=5, width=50)
        self.taxa_text.pack(fill="both", expand=True, pady=5)
        
        # Load G064 preset
        self.load_preset()
        
        # Console output
        console_frame = ttk.LabelFrame(main_frame, text="Console Output", padding="10")
        console_frame.grid(row=4, column=0, columnspan=3, sticky="nsew", pady=10)
        main_frame.rowconfigure(4, weight=1)
        
        self.console = scrolledtext.ScrolledText(console_frame, height=15, width=80, 
                                                state="disabled", wrap="word")
        self.console.pack(fill="both", expand=True)
        
        # Button frame
        button_frame = ttk.Frame(main_frame)
        button_frame.grid(row=5, column=0, columnspan=3, sticky="ew", pady=10)
        
        self.run_button = ttk.Button(button_frame, text="Run", command=self.run_workflow)
        self.run_button.pack(side="left", padx=5)
        
        self.cancel_button = ttk.Button(button_frame, text="Cancel", command=self.cancel_workflow)
        self.cancel_button.pack(side="left", padx=5)
        self.cancel_button.config(state="disabled")
        
        ttk.Button(button_frame, text="Clear", command=self.clear_console).pack(side="left", padx=5)
        
        self.update_ui()
    
    def update_ui(self):
        """Update UI based on workflow selection."""
        workflow = self.workflow_var.get()
        
        if workflow == "download":
            self.species_frame.grid()
            self.data_frame.grid_remove()
            self.phase_frame.grid_remove()
        elif workflow == "prepare":
            self.species_frame.grid_remove()
            self.data_frame.grid(row=2, column=0, columnspan=3, sticky="ew", pady=10)
            self.phase_frame.grid(row=3, column=0, columnspan=3, sticky="ew", pady=10)
        else:  # full
            self.species_frame.grid()
            self.data_frame.grid_remove()
            self.phase_frame.grid_remove()
    
    def load_preset(self):
        """Load filter preset."""
        preset_name = self.preset_var.get()
        preset = PRESETS.get(preset_name, {})
        
        if preset_name == "G064 (Default)":
            self.species_entry.delete(0, tk.END)
            self.species_entry.insert(0, preset.get("species", ""))
            
            self.precision_var.set("relaxed" if preset.get("relax_precision", True) else "strict")
            
            taxa = preset.get("exclude_taxa", [])
            self.taxa_text.config(state="normal")
            self.taxa_text.delete(1.0, tk.END)
            self.taxa_text.insert(1.0, "\n".join(taxa))
            self.taxa_text.config(state="disabled")
    
    def browse_data(self):
        """Browse for data folder or ZIP file."""
        path = filedialog.askdirectory(title="Select data folder")
        if not path:
            path = filedialog.askopenfilename(title="Select ZIP file", 
                                             filetypes=[("ZIP files", "*.zip"), ("All files", "*.*")])
        if path:
            self.data_entry.delete(0, tk.END)
            self.data_entry.insert(0, path)
    
    def log(self, message):
        """Log message to console."""
        self.console.config(state="normal")
        self.console.insert(tk.END, message + "\n")
        self.console.see(tk.END)
        self.console.config(state="disabled")
        self.root.update()
    
    def clear_console(self):
        """Clear console output."""
        self.console.config(state="normal")
        self.console.delete(1.0, tk.END)
        self.console.config(state="disabled")
    
    def run_workflow(self):
        """Run the selected workflow."""
        self.run_button.config(state="disabled")
        self.cancel_button.config(state="normal")
        self.is_running = True
        
        workflow = self.workflow_var.get()
        
        if workflow == "download":
            thread = threading.Thread(target=self.run_download)
        elif workflow == "prepare":
            thread = threading.Thread(target=self.run_prepare)
        else:  # full
            thread = threading.Thread(target=self.run_full)
        
        thread.daemon = True
        thread.start()
    
    def run_download(self):
        """Run download workflow."""
        try:
            species = self.species_entry.get().strip()
            if not species:
                self.log("ERROR: Please enter a species name")
                return
            
            self.log(f"\n")
            self.log(f"DOWNLOADING: {species}")
            self.log(f"\n")
            
            taxon_key = resolve_species(species)
            queries = [
                f"taxonKey = {taxon_key}",
                "basisOfRecord = PRESERVED_SPECIMEN",
                "hasCoordinate = True",
                "occurrenceStatus = PRESENT",
            ]
            
            dl_key = trigger_download(queries, "DWCA", GBIF_USER, GBIF_PASSWORD, GBIF_EMAIL)
            self.log(f"Download key: {dl_key}")
            self.log("Waiting for GBIF (5-30 minutes)...\n")
            
            zip_file = wait_and_download(dl_key)
            extract_folder = extract_archive(zip_file, f"./data_{dl_key}")
            
            self.log(f"\nExtracted to: {extract_folder}")
            messagebox.showinfo("Complete", f"Download complete!\nLocation: {extract_folder}")
        
        except Exception as e:
            self.log(f"ERROR: {str(e)}")
            messagebox.showerror("Error", str(e))
        
        finally:
            self.run_button.config(state="normal")
            self.cancel_button.config(state="disabled")
            self.is_running = False
    
    def run_prepare(self):
        """Run prepare workflow."""
        try:
            data_path = self.data_entry.get().strip()
            if not data_path:
                self.log("ERROR: Please select data folder or ZIP file")
                return
            
            # Handle ZIP file
            if os.path.isfile(data_path) and data_path.endswith('.zip'):
                self.log("Extracting ZIP file...")
                import zipfile
                extract_name = Path(data_path).stem
                with zipfile.ZipFile(data_path, 'r') as zip_ref:
                    zip_ref.extractall(extract_name)
                data_path = extract_name
                self.log(f"Extracted to: {data_path}\n")
            
            if not os.path.isdir(data_path):
                self.log("ERROR: Invalid data folder")
                return
            
            # Get filters
            exclude_taxa = [line.strip() for line in self.taxa_text.get(1.0, tk.END).split('\n') if line.strip()]
            relax_precision = self.precision_var.get() == "relaxed"
            phase = self.phase_var.get()
            
            if phase in ["both", "phase1"]:
                self.log(f"\n")
                self.log("PHASE 1: AUTOMATED CLEANING")
                self.log(f"\n")
                
                occurrence_file = os.path.join(data_path, "occurrence.txt")
                multimedia_file = os.path.join(data_path, "multimedia.txt")
                
                if not os.path.exists(occurrence_file) or not os.path.exists(multimedia_file):
                    self.log("ERROR: occurrence.txt or multimedia.txt not found")
                    return
                
                phase_1_clean_and_merge(
                    occurrence_file,
                    multimedia_file,
                    output_csv="GBIFdownload_inspectFlags.csv",
                    exclude_taxa=exclude_taxa if exclude_taxa else None,
                    relax_coordinate_precision=relax_precision
                )
                
                self.log("\nPhase 1 complete! Edit GBIFdownload_inspectFlags.csv in Excel")
                
                if phase == "both":
                    response = messagebox.askyesno("Proceed", "Run Phase 2?")
                    if response:
                        self.log(f"\n")
                        self.log("PHASE 2: FINALIZATION")
                        self.log(f"\n")
                        
                        phase_2_finalize_dataset(
                            "GBIFdownload_inspectFlags.csv",
                            final_master="master_cleaned.csv",
                            final_duplicates="removed_duplicates.csv"
                        )
                        self.log("\nPhase 2 complete! Ready for ImageJ")
                        messagebox.showinfo("Complete", "Phase 2 complete!\nMaster: master_cleaned.csv\nDuplicates: removed_duplicates.csv")
            
            elif phase == "phase2":
                self.log(f"\n")
                self.log("PHASE 2: FINALIZATION")
                self.log(f"\n")
                
                phase_2_finalize_dataset(
                    "GBIFdownload_inspectFlags.csv",
                    final_master="master_cleaned.csv",
                    final_duplicates="removed_duplicates.csv"
                )
                self.log("\nPhase 2 complete!")
                messagebox.showinfo("Complete", "Phase 2 complete!")
        
        except Exception as e:
            self.log(f"ERROR: {str(e)}")
            messagebox.showerror("Error", str(e))
        
        finally:
            self.run_button.config(state="normal")
            self.cancel_button.config(state="disabled")
            self.is_running = False
    
    def run_full(self):
        """Run full workflow."""
        try:
            species = self.species_entry.get().strip()
            if not species:
                self.log("ERROR: Please enter a species name")
                return
            
            # Get filters
            exclude_taxa = [line.strip() for line in self.taxa_text.get(1.0, tk.END).split('\n') if line.strip()]
            relax_precision = self.precision_var.get() == "relaxed"
            
            # Download
            self.log(f"\n")
            self.log(f"STEP 1: DOWNLOADING {species}")
            self.log(f"\n")
            
            taxon_key = resolve_species(species)
            queries = [
                f"taxonKey = {taxon_key}",
                "basisOfRecord = PRESERVED_SPECIMEN",
                "hasCoordinate = True",
                "occurrenceStatus = PRESENT",
            ]
            
            dl_key = trigger_download(queries, "DWCA", GBIF_USER, GBIF_PASSWORD, GBIF_EMAIL)
            self.log(f"Download key: {dl_key}")
            self.log("Waiting for GBIF (5-30 minutes)...\n")
            
            zip_file = wait_and_download(dl_key)
            extract_folder = extract_archive(zip_file, f"./data_{dl_key}")
            
            # Phase 1
            self.log(f"\n")
            self.log("STEP 2: PHASE 1 CLEANING")
            self.log(f"\n")
            
            occurrence_file = os.path.join(extract_folder, "occurrence.txt")
            multimedia_file = os.path.join(extract_folder, "multimedia.txt")
            
            phase_1_clean_and_merge(
                occurrence_file,
                multimedia_file,
                output_csv="GBIFdownload_inspectFlags.csv",
                exclude_taxa=exclude_taxa if exclude_taxa else None,
                relax_coordinate_precision=relax_precision
            )
            
            self.log("\nPhase 1 complete! Edit GBIFdownload_inspectFlags.csv in Excel")
            response = messagebox.askyesno("Manual Review", "Edit the CSV file and click Yes to continue with Phase 2")
            
            if response:
                self.log(f"\n")
                self.log("STEP 3: PHASE 2 FINALIZATION")
                self.log(f"\n")
                
                phase_2_finalize_dataset(
                    "GBIFdownload_inspectFlags.csv",
                    final_master="master_cleaned.csv",
                    final_duplicates="removed_duplicates.csv"
                )
                
                self.log("\nFull workflow complete! Ready for ImageJ")
                messagebox.showinfo("Complete", "Workflow complete!\nMaster: master_cleaned.csv")
        
        except Exception as e:
            self.log(f"ERROR: {str(e)}")
            messagebox.showerror("Error", str(e))
        
        finally:
            self.run_button.config(state="normal")
            self.cancel_button.config(state="disabled")
            self.is_running = False
    
    def cancel_workflow(self):
        """Cancel workflow."""
        self.is_running = False
        self.log("\nWorkflow cancelled")
        self.run_button.config(state="normal")
        self.cancel_button.config(state="disabled")


# ==============
# CLI FUNCTIONS
# ==============
    """Print welcome banner."""
    print("\n" + "="*60)
    print("GBIF HERBARIA DATA PIPELINE - CLI")
    print("="*60 + "\n")


def download_workflow():
    """Download only workflow."""
    print("DOWNLOAD ONLY")
    print("-"*60)
    species = input("\nEnter species name (e.g., 'Megathyrsus maximus'): ").strip()
    
    if not species:
        print("ERROR: Species name required")
        return
    
    try:
        print(f"\nDownloading {species}...")
        taxon_key = resolve_species(species)
        
        queries = [
            f"taxonKey = {taxon_key}",
            "basisOfRecord = PRESERVED_SPECIMEN",
            "hasCoordinate = True",
            "occurrenceStatus = PRESENT",
        ]
        
        dl_key = trigger_download(queries, "DWCA", GBIF_USER, GBIF_PASSWORD, GBIF_EMAIL)
        print(f"Download key: {dl_key}\nWaiting for GBIF (5-30 minutes)...")
        
        zip_file = wait_and_download(dl_key)
        extract_folder = extract_archive(zip_file, f"./data_{dl_key}")
        
        print(f"\n✓ Download complete!\nLocation: {extract_folder}\n")
    
    except Exception as e:
        print(f"ERROR: {str(e)}\n")


def prepare_workflow():
    """Prepare/clean only workflow."""
    print("PREPARE ONLY (CLEANING)")
    print("-"*60)
    
    data_path = input("\nEnter data folder path (or ZIP file path): ").strip()
    
    if not data_path:
        print("ERROR: Data path required")
        return
    
    # Handle ZIP file
    if os.path.isfile(data_path) and data_path.endswith('.zip'):
        print("\nExtracting ZIP file...")
        import zipfile
        extract_name = Path(data_path).stem
        try:
            with zipfile.ZipFile(data_path, 'r') as zip_ref:
                zip_ref.extractall(extract_name)
            data_path = extract_name
            print(f"Extracted to: {data_path}")
        except Exception as e:
            print(f"ERROR: Failed to extract ZIP: {str(e)}\n")
            return
    
    if not os.path.isdir(data_path):
        print("ERROR: Invalid data folder path\n")
        return
    
    print("\nPhase options:")
    print("  1 - Phase 1 & 2 (Full cleaning)")
    print("  2 - Phase 1 only (For manual inspection)")
    print("  3 - Phase 2 only (After manual review)")
    
    choice = input("\nSelect phase (1-3): ").strip()
    
    try:
        if choice in ["1", "2", "3"]:
            occurrence_file = os.path.join(data_path, "occurrence.txt")
            multimedia_file = os.path.join(data_path, "multimedia.txt")
            
            if choice in ["1", "2"]:
                if not os.path.exists(occurrence_file) or not os.path.exists(multimedia_file):
                    print("ERROR: occurrence.txt or multimedia.txt not found\n")
                    return
                
                print("\nPhase 1: Automated cleaning...")
                phase_1_clean_and_merge(
                    occurrence_file,
                    multimedia_file,
                    output_csv="GBIFdownload_inspectFlags.csv",
                    exclude_taxa=EXCLUDE_TAXA,
                    relax_coordinate_precision=True
                )
                
                print("✓ Phase 1 complete!")
                print("✓ File: GBIFdownload_inspectFlags.csv")
                print("  → Edit this file in Excel (remove suspicious records)")
                
                if choice == "1":
                    response = input("\nProceed to Phase 2? (y/n): ").strip().lower()
                    if response == "y":
                        print("\nPhase 2: Finalization & deduplication...")
                        phase_2_finalize_dataset(
                            "GBIFdownload_inspectFlags.csv",
                            final_master="master_cleaned.csv",
                            final_duplicates="removed_duplicates.csv"
                        )
                        print("✓ Phase 2 complete!")
                        print("✓ Master: master_cleaned.csv (use for ImageJ)")
                        print("✓ Duplicates: removed_duplicates.csv\n")
            
            elif choice == "3":
                print("\nPhase 2: Finalization & deduplication...")
                phase_2_finalize_dataset(
                    "GBIFdownload_inspectFlags.csv",
                    final_master="master_cleaned.csv",
                    final_duplicates="removed_duplicates.csv"
                )
                print("✓ Phase 2 complete!")
                print("✓ Master: master_cleaned.csv")
                print("✓ Duplicates: removed_duplicates.csv\n")
        else:
            print("ERROR: Invalid selection\n")
    
    except Exception as e:
        print(f"ERROR: {str(e)}\n")


def full_workflow():
    """Full workflow: download + prepare."""
    print("FULL WORKFLOW (DOWNLOAD + PREPARE)")
    print("-"*60)
    
    species = input("\nEnter species name (e.g., 'Megathyrsus maximus'): ").strip()
    
    if not species:
        print("ERROR: Species name required")
        return
    
    try:
        # Download
        print(f"\nStep 1: Downloading {species}...")
        taxon_key = resolve_species(species)
        
        queries = [
            f"taxonKey = {taxon_key}",
            "basisOfRecord = PRESERVED_SPECIMEN",
            "hasCoordinate = True",
            "occurrenceStatus = PRESENT",
        ]
        
        dl_key = trigger_download(queries, "DWCA", GBIF_USER, GBIF_PASSWORD, GBIF_EMAIL)
        print(f"Download key: {dl_key}\nWaiting for GBIF (5-30 minutes)...")
        
        zip_file = wait_and_download(dl_key)
        extract_folder = extract_archive(zip_file, f"./data_{dl_key}")
        
        # Phase 1
        print("\nStep 2: Phase 1 - Automated cleaning...")
        occurrence_file = os.path.join(extract_folder, "occurrence.txt")
        multimedia_file = os.path.join(extract_folder, "multimedia.txt")
        
        phase_1_clean_and_merge(
            occurrence_file,
            multimedia_file,
            output_csv="GBIFdownload_inspectFlags.csv",
            exclude_taxa=EXCLUDE_TAXA,
            relax_coordinate_precision=True
        )
        
        print("✓ Phase 1 complete!")
        print("→ File: GBIFdownload_inspectFlags.csv")
        print("→ Edit this file in Excel before continuing")
        
        input("\nPress Enter after editing the CSV file...")
        
        # Phase 2
        print("\nStep 3: Phase 2 - Finalization...")
        phase_2_finalize_dataset(
            "GBIFdownload_inspectFlags.csv",
            final_master="master_cleaned.csv",
            final_duplicates="removed_duplicates.csv"
        )
        
        print("\n" + "="*60)
        print("✓ WORKFLOW COMPLETE!")
        print("="*60)
        print("\nOutput files:")
        print("  • master_cleaned.csv     ← Use for ImageJ measurements")
        print("  • removed_duplicates.csv ← Backup records\n")
    
    except Exception as e:
        print(f"ERROR: {str(e)}\n")


def main():
    """Main CLI menu."""
    print_banner()
    
    print("Select workflow:")
    print("  1 - Download only")
    print("  2 - Prepare only (clean existing data)")
    print("  3 - Full workflow (download + prepare)")
    print()
    
    choice = input("Enter choice (1-3): ").strip()
    
    if choice == "1":
        download_workflow()
    elif choice == "2":
        prepare_workflow()
    elif choice == "3":
        full_workflow()
    else:
        print("ERROR: Invalid choice\n")


def launch_gui():
    """Launch the GUI application."""
    root = tk.Tk()
    app = GBIFPipelineGUI(root)
    root.mainloop()


def launcher():
    """Main launcher menu - choose between GUI and CLI."""
    print("\n" + "="*60)
    print("GBIF HERBARIA DATA PIPELINE")
    print("="*60)
    print()
    print("Select interface:")
    print("  1 - GUI (Graphical, recommended)")
    print("  2 - CLI (Command line)")
    print()
    
    choice = input("Enter choice (1 or 2): ").strip()
    
    if choice == "1":
        launch_gui()
    elif choice == "2":
        main()
    else:
        print("ERROR: Invalid choice\n")


if __name__ == "__main__":
    launcher()