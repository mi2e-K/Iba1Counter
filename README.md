# Iba1Counter

Semi-automated **Fiji + Python** pipeline for counting Iba1+ microglia in
fluorescence microscopy images.

The algorithm is **soma-oriented**: instead of thresholding the entire Iba1+
signal (which merges adjacent cells through processes or counts process
fragments as cells), it suppresses thin processes, detects cell-body
candidates as DoG blobs at a **fixed absolute threshold**, and grows tight
regions via marker-controlled watershed. Per-image adaptive thresholds are
deliberately avoided — they inflate false positives in microglia-depleted
samples and erase group differences.

## Outputs

| Type | Contents |
|---|---|
| **Primary** | Cell density (cells/mm²) |
| Supporting | Iba1+ area fraction, mean & integrated fluorescence |
| Per-cell | Centroid, area (µm²), mean / peak / integrated intensity |
| QC | Annotated overlay PNGs (full + clean variants) |

Morphology classification (Sholl, ramified / activated / amoeboid) is
**not** included by design.

## Install

```bash
git clone https://github.com/<user>/Iba1Counter.git
cd Iba1Counter
pip install -r requirements.txt
```

Requires Python 3.10+. Dependencies: `numpy`, `scipy`, `scikit-image`,
`tifffile`, `imagecodecs`, `pandas`, `matplotlib`, `roifile`, `PyYAML`.

## Quick start

### Option A — From Fiji (recommended for non-Python users)

1. Copy `fiji_macro/Iba1Counter.ijm` to `<Fiji.app>/plugins/` and restart Fiji.
2. `Plugins ▸ Iba1 Counter` → tick **Configure paths** once and point at your
   Python and `analyze_iba1_microglia.py`.
3. Walk through the menu: *Setup project* → *Draw ROIs* → *Run analysis* →
   *Review QC overlays* → optional *Apply manual corrections*.

Full macro guide: [`fiji_macro/README.md`](fiji_macro/README.md).

### Option B — CLI

```bash
# Standard batch
python analyze_iba1_microglia.py --config config.yaml

# With validation against manual counts
python analyze_iba1_microglia.py --config config.yaml --validate

# Grid-search optimization
python analyze_iba1_microglia.py --config config.yaml --optimize

# Single image
python analyze_iba1_microglia.py --config config.yaml --single-image path/to/img.tif
```

Start from the annotated [`config_example.yaml`](config_example.yaml).

## Pipeline

```
Image (TIFF / RGB / multi-channel)
   ↓ extract Iba1 channel
Background correction (rolling-ball | morph opening | gaussian | external)
   ↓
Mild denoising (median / gaussian)
   ↓
Auto hole / edge suppression (gradient + dark-region masks, optional)
   ↓
Soma enhancement (morphological opening + DoG / LoG / top-hat)
   ↓
Seed detection (peak_local_max with FIXED absolute threshold; multiscale optional)
   ↓
Marker-controlled watershed restricted to soma mask
   ↓ + per-seed distance cap
Object filtering (area + intensity + shape + intensity-uniformity;
                  per-candidate exemption for enlarged bright soma)
   ↓
CSVs + per-image QC overlays
```

## Output layout

```
<project>/
├── config.yaml             # parameters used for the run
├── input/                  # your TIFFs
├── rois/                   # Fiji ROI Manager .zip per image
└── output/
    ├── detection_summary.csv  # one row per (image, ROI) — primary result
    ├── per_object.csv      # one row per candidate (accepted + rejected)
    ├── parameters.yaml     # frozen config + library versions (reproducible)
    ├── run.log
    └── qc_overlays/
        ├── <image>__<roi>.png   # full overlay (raw + ROI + accepts + rejects + scale bar)
        └── <image>_qc.png        # clean overlay (raw image + green LUT + semi-transparent cyan circles)
```

## Tuning

Detection parameters are **fixed within a staining/imaging batch** and
re-tuned across batches. To tune:

1. Pick 6–12 representative images (include depleted samples and edge cases).
2. Manually count → write `manual_counts.csv`.
3. Enable optimization in `config.yaml`:
   ```yaml
   optimization:
     enabled: true
     manual_counts_csv: path/to/manual_counts.csv
     metric: mae_balanced     # avoids dominance by control group
     grids:
       soma_radius_px:        [2.0, 3.0, 4.0]
       min_peak_intensity:    [5.0, 10.0, 15.0]
       min_distance_px:       [3.0, 4.0, 6.0]
   ```
4. Run with `--optimize` and adopt `output/optimization/best_config.yaml`.
5. Apply to the full batch with a tagged `parameter_set_id`.

Detailed strategy: [`docs/parameter_tuning.md`](docs/parameter_tuning.md).

## Validation

```bash
python analyze_iba1_microglia.py --config config.yaml --validate
```

Produces scatter plot, Bland–Altman plot, per-group MAE / RMSE / signed bias.
Detailed plan: [`docs/validation.md`](docs/validation.md).

## Manual correction

After running, open QC overlays in Fiji, run `6. Apply manual corrections`
in the macro to add missed cells / remove false positives via the
Multi-point tool. Both raw automated `count` and post-review
`count_corrected` are preserved for audit.

Correction ROIs can also be supplied directly in the config via
`corrections.remove_roi_directory` and `corrections.add_roi_directory`.
Point ROIs remove/add individual detections; polygon/oval remove ROIs remove
all accepted detection centroids inside the marked region.

## Methods text (paste-ready)

> Iba1+ microglia were quantified using Iba1Counter, a semi-automated
> Fiji/Python pipeline (https://github.com/mi2e-K/Iba1Counter). ROIs were
> manually drawn in Fiji. Within each ROI, tissue holes and sharp
> intensity gradients were auto-detected from the raw image and masked
> out prior to detection. Cell bodies were then detected by background
> correction, soma-enhancing DoG filtering, fixed-threshold blob seed
> detection, and marker-controlled watershed. Candidates were accepted
> as microglia when they met absolute thresholds on area, mean and peak
> intensity.
> All thresholds were fixed within
> each staining/imaging batch so that intergroup density differences reflect biology
> rather than per-image rescaling. Density
> (cells/mm²) and Iba1+ area fraction are reported.


## Repository layout

```
analyze_iba1_microglia.py    # CLI entry point
config_example.yaml          # annotated config template
iba1_pipeline/               # core Python package
fiji_macro/                  # Fiji front-end (.ijm + guide)
examples/                    # CSV templates (manual counts, corrections, metadata)
docs/                        # extended docs
tests/                       # smoke tests + macro syntax sweeps
```

## License

MIT

## Citation

If you use Iba1Counter in published work, please cite this repository and
the relevant Methods text above.
