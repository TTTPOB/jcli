# Create a notebook

Use this workflow to create notebook content, not a kernel session.

1. Choose a new py:percent path such as `analysis.py`; do not overwrite an existing pair.
2. Set kernelspec metadata only when the intended kernel is known. If execution is needed and the kernel is unclear, follow [session](session.md).
3. Write the notebook as text using the format below. Use separate cells for meaningful steps.
4. End cells that should display a table or figure with a bare `df` or `fig` expression by default. Do not add `fig.show()`, `plt.show()`, or `display(fig)` merely to render the final figure.
5. Let the configured hook synchronize the pair. Without that hook, use `j-cli convert py-to-ipynb analysis.py analysis.ipynb` to create the notebook without execution.
6. Only if execution is requested, load [exec](exec.md). File execution can also create the paired notebook automatically; no separate conversion is needed first.

## Notebook template

j-cli supports py:percent format — plain Python files with `# %%` cell markers:

```python
# ---
# jupyter:
#   kernelspec:
#     name: python3
# ---

# %% id="imports"
import matplotlib.pyplot as plt
import numpy as np

# %% id="plot"
x = np.linspace(0, 10, 100)
fig, ax = plt.subplots()
ax.plot(x, np.sin(x))
fig

# %% [markdown] id="results"
# ## Results
# The plot above shows a sine wave.
```

Cell markers may carry a stable nbformat ID as `id="..."`. Preserve the ID when
editing or moving an existing cell. Hook synchronization will assign an ID to a
newly inserted cell, so you don't have to assign it manually.

## Format details

j-cli comments IPython magic commands in py:percent files so Python tools can
parse them, then restores the commands when syncing to `.ipynb`. Python-body
cell magics such as `%%timeit` and `%%writefile` keep their body as Python code;
other cell magics are commented through the end of the cell.

A plain Python script without cell markers or notebook front matter is not a py:percent notebook. Do not start a server or create a session just to write notebook content.
