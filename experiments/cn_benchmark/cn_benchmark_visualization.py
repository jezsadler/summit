from summit import Runner
from summit.utils.multiobjective import pareto_efficient, hypervolume

from neptune.sessions import Session, HostedNeptuneBackend
from pymoo.model.problem import Problem
import pandas as pd
import numpy as np

import matplotlib.pyplot as plt
from matplotlib import cm
import matplotlib as mpl
from matplotlib.colors import ListedColormap, LinearSegmentedColormap
import plotly.graph_objects as go
import plotly.express as px
from pandas.plotting import parallel_coordinates

import os
import zipfile
import shutil
import warnings
from textwrap import wrap
import collections


def flatten(d, parent_key="", sep="_"):
    items = []
    for k, v in d.items():
        new_key = parent_key + sep + k if parent_key else k
        if isinstance(v, collections.MutableMapping):
            items.extend(flatten(v, new_key, sep=sep).items())
        else:
            items.append((new_key, v))
    return dict(items)


def determine_pareto_front(n_points=5000, random_seed=100):
    exp = SnarBenchmark()
    rand = Random(exp.domain, random_state=np.random.RandomState(random_seed))
    experiments = rand.suggest_experiments(n_points)
    exp.run_experiments(experiments)
    return exp


class DomainWrapper(Problem):
    """ Wrapper for NSGAII optimisation of a `summit.experiment.Experiment` using pymoo
    
    Parameters
    ---------- 
    models : :class:`~summit.utils.models.ModelGroup`
        Model group used for optimisation
    domain : :class:`~summit.domain.Domain`
        Domain used for optimisation.
    Notes
    -----
    It is assumed that the inputs are scaled between 0 and 1.
    
    """

    def __init__(self, experiment):
        self.experiment = experiment
        self.domain = self.experiment.domain
        # Number of decision variables
        n_var = self.domain.num_continuous_dimensions()
        # Number of objectives
        n_obj = len(self.domain.output_variables)
        # Number of constraints
        n_constr = len(self.domain.constraints)
        # Lower bounds
        xl = [v.bounds[0] for v in self.domain.input_variables]
        # Upper bounds
        xu = [v.bounds[1] for v in self.domain.input_variables]
        self.input_columns = [v.name for v in self.domain.input_variables]
        self.output_columns = [v.name for v in self.domain.output_variables]

        super().__init__(n_var=n_var, n_obj=n_obj, n_constr=n_constr, xl=xl, xu=xu)

    def _evaluate(self, X, out, *args, **kwargs):
        X = DataSet(np.atleast_2d(X), columns=self.input_columns)
        X[("strategy", "METADATA")] = "NSGAII"
        F = self.experiment.run_experiments(X)
        F = F[self.output_columns].data_to_numpy()

        # Negate objectives that  need to be maximized
        for i, v in enumerate(self.domain.output_variables):
            if v.maximize:
                F[:, i] *= -1
        out["F"] = F

        # Add constraints if necessary
        if self.domain.constraints:
            constraint_res = [
                X.eval(c.lhs, resolvers=[X]) for c in self.domain.constraints
            ]
            out["G"] = [c.tolist()[0] for c in constraint_res]


COLORS = [
    (165, 0, 38),
    (215, 48, 39),
    (244, 109, 67),
    (253, 174, 97),
    (254, 224, 144),
    (255, 255, 191),
    (224, 243, 248),
    (171, 217, 233),
    (116, 173, 209),
    (69, 117, 180),
    (49, 54, 149),
]
COLORS = np.array(COLORS) / 256
CMAP = ListedColormap(COLORS)


class PlotExperiments:
    """  Make plots from benchmarks tracked on Neptune
    
    Parameters
    ---------- 
    project : str
        The name of the Neptune.ai project
    experiment_ids : list of str
        A list of experiment ids to pull from Neptune.ai
    tag : list  of str, optional
        A list of tags used as filters 
    state : str, optional
        The state of the experiments. Must be succeeded, failed, running or aborted.
    trajectory_length : int, optional
        The maximum number of iterations for each experiment. Defaults to 50.
    num_repeats : int, optional
        The number of repeats required for each hyperparameter combination.s
        
    """

    def __init__(
        self,
        project: str,
        experiment_ids: list,
        tag: list = None,
        state: str = None,
        trajectory_length=50,
        num_repeats=20,
    ):
        self.session = Session(backend=HostedNeptuneBackend())
        self.proj = self.session.get_project(project)
        self.runners = {}
        self.experiment_ids = experiment_ids
        self.tag = tag
        self.state = state
        self.trajectory_length = trajectory_length
        self.num_repeats = num_repeats
        self._restore_runners()
        self._create_param_df()

    def _restore_runners(self):
        """Restore runners from Neptune Artifacts"""
        # Download artifacts
        n_experiments = len(self.experiment_ids)
        experiments = []
        if n_experiments > 100:
            for i in range(n_experiments // 100):
                experiments += self.proj.get_experiments(
                    id=self.experiment_ids[i * 100 : (i + 1) * 100],
                    tag=self.tag,
                    state=self.state,
                )
            remainder = n_experiments % 100
            experiments += self.proj.get_experiments(
                id=self.experiment_ids[(i + 1) * 100 : (i + 1) * 100 + remainder],
                tag=self.tag,
                state=self.state,
            )
        else:
            experiments = self.proj.get_experiments(
                id=self.experiment_ids, tag=self.tag, state=self.state
            )
        for experiment in experiments:
            path = f"data/{experiment.id}"
            try:
                os.mkdir(path,)
            except FileExistsError:
                pass
            experiment.download_artifacts(destination_dir=path)

            # Unzip somehow
            files = os.listdir(path)
            with zipfile.ZipFile(path + "/" + files[0], "r") as zip_ref:
                zip_ref.extractall(path)

            # Get filename
            path += "/output"
            files = os.listdir(path)
            files_json = [f for f in files if ".json" in f]
            if len(files_json) == 0:
                warnings.warn(f"{experiment.id} has no file attached.")
                continue

            # Restore runner
            r = Runner.load(path + "/" + files_json[0])
            self.runners[experiment.id] = r

            # Remove file
            shutil.rmtree(f"data/{experiment.id}")

    def _create_param_df(self, reference=[0, 1]):
        """Create a parameters dictionary
        
        Parameters
        ----------
        reference : array-like, optional
            Reference for the hypervolume calculatio

        """
        records = []
        for experiment_id, r in self.runners.items():
            record = {}
            record["experiment_id"] = experiment_id

            # Transform
            transform_name = r.strategy.transform.__class__.__name__
            transform_params = r.strategy.transform.to_dict()["transform_params"]
            record["transform_name"] = transform_name
            if transform_name == "Chimera":
                hierarchy = transform_params["hierarchy"]
                for objective_name, v in hierarchy.items():
                    key = f"{objective_name}_tolerance"
                    record[key] = v["tolerance"]
            elif transform_name == "MultitoSingleObjective":
                record.update(transform_params)

            # Strategy
            record["strategy_name"] = r.strategy.__class__.__name__

            # Batch size
            record["batch_size"] = r.batch_size

            # Number of initial experiments
            # try:
            #     record["num_initial_experiments"] = r.n_init
            # except AttributeError:
            #     pass

            # Terminal hypervolume
            data = r.experiment.data[["yld", "cost"]].to_numpy()
            data[:, 0] *= -1  # make it a minimzation problem
            y_front, _ = pareto_efficient(
                data[: self.trajectory_length, :], maximize=False
            )
            hv = hypervolume(y_front, ref=reference)
            record["terminal_hypervolume"] = hv

            # Computation time
            time = (
                r.experiment.data["computation_t"]
                .iloc[0 : self.trajectory_length]
                .sum()
            )
            record["computation_t"] = time

            # Use descriptors
            try:
                descriptors = r.strategy.use_descriptors
            except AttributeError:
                descriptors = None
            record["use_descriptors"] = descriptors

            records.append(record)

        # Make pandas dataframe
        self.df = pd.DataFrame.from_records(records)
        return self.df

    def best_pareto_grid(self, ncols=3, figsize=(20, 40)):
        """ Make a grid of pareto plots 

        Only includes the run with the maximum terminal hypervolume for each 
        unique hyperparameter combination.

        Parameters
        ----------
        ncols : int, optional
            The number of columns in the grid. Defaults to 3
        figsize : tuple, optional
            The figure size. Defaults to 20 wide x 40 high

        """
        # Group experiment repeats
        df = self.df.copy()
        df = df.set_index("experiment_id")
        df = df.drop(columns=["terminal_hypervolume", "computation_t"])
        uniques = df.drop_duplicates(keep="last")  # This finds the unique combinations
        uniques = uniques.sort_values(by=["strategy_name", "transform_name"])
        df_new = self.df.copy()

        # Make matplotlib figure
        nrows = len(uniques) // ncols
        nrows += 1 if len(uniques) % ncols != 0 else 0
        fig = plt.figure(figsize=figsize)
        fig.subplots_adjust(wspace=0.2, hspace=0.5)

        # Loop through groups of repeats and create figures
        i = 1
        for index, unique in uniques.iterrows():
            # Find number of matching rows to this unique row
            temp_df = df_new.merge(unique.to_frame().transpose(), how="inner")

            # Find experiment with maximum hypervolume
            max_hv_index = temp_df["terminal_hypervolume"].argmax()
            experiment_id = temp_df.iloc[max_hv_index]["experiment_id"]

            # Get runner
            r = self.runners[experiment_id]

            # Create pareto plot
            ax = plt.subplot(nrows, ncols, i)
            old_data = r.experiment._data.copy()
            r.experiment._data = r.experiment.data.iloc[: self.trajectory_length, :]
            r.experiment.pareto_plot(ax=ax)
            r.experiment._data = old_data
            title = self._create_label(unique)
            title = "\n".join(wrap(title, 30))
            ax.set_title(title)
            ax.set_xlabel("Yield")
            ax.set_ylabel("Cost")
            ax.set_xlim(0.0, 1)
            ax.set_ylim(0.0, 1)
            ax.ticklabel_format(axis="x", style="scientific")
            i += 1

        return fig

    def plot_hv_trajectories(
        self,
        reference=[0, 1],
        plot_type="matplotlib",
        include_experiment_ids=False,
        min_terminal_hv_avg=0,
        ax=None,
    ):
        """ Plot hypervolume vs number of experiments with repeats as 95% confidence interval
        
        Parameters
        ----------
        reference : array-like, optional
            Reference for the hypervolume calculation. Defaults to 0, 1
        plot_type : str, optional
            Plotting backend to use: matplotlib or plotly. Defaults to matplotlib.
        include_experiment_ids : bool, optional
            Whether to include experiment ids in the plot labels
        min_terminal_hv_avg : float, optional`
            Minimum terminal average hypervolume cutoff for inclusion in the plot. Defaults to 0.
        ax : `matplotlib.pyplot.axes`, optional
            Matplotlib axis for plotting
        """
        # Create figure
        if plot_type == "matplotlib":
            if ax is not None:
                fig = None
            else:
                fig, ax = plt.subplots(1)
        elif plot_type == "plotly":
            fig = go.Figure()
        else:
            raise ValueError(
                f"{plot_type} is not a valid plot type. Must be matplotlib or plotly."
            )

        # Group experiment repeats
        df = self.df.copy()
        df_new = self.df.copy()
        df = df.set_index("experiment_id")
        df = df.drop(columns=["terminal_hypervolume", "computation_t"])
        uniques = df.drop_duplicates(
            keep="last"
        )  # This finds all the unique combinations

        # Colors
        if plot_type == "plotly":
            colors = px.colors.qualitative.Plotly
        else:
            colors = COLORS
        cycle = len(colors)
        c_num = 0

        # Plots for each unique hyperparameter combination
        for index, unique in uniques.iterrows():
            # Find number of matching rows to this unique row
            temp_df = df_new.merge(unique.to_frame().transpose(), how="inner")
            ids = temp_df["experiment_id"].values

            # Calculate hypervolume trajectories
            ids = ids if len(ids) <= self.num_repeats else ids[: self.num_repeats - 1]
            hv_trajectories = np.zeros([self.trajectory_length, len(ids)])
            for j, experiment_id in enumerate(ids):
                r = self.runners[experiment_id]
                data = r.experiment.data[["yld", "cost"]].to_numpy()
                data[:, 0] *= -1  # make it a minimzation problem
                for i in range(self.trajectory_length):
                    y_front, _ = pareto_efficient(data[0 : i + 1, :], maximize=False)
                    hv_trajectories[i, j] = hypervolume(y_front, ref=reference)

            # Mean and standard deviation
            hv_mean_trajectory = np.mean(hv_trajectories, axis=1)
            hv_std_trajectory = np.std(hv_trajectories, axis=1)

            if hv_mean_trajectory[-1] < min_terminal_hv_avg:
                continue

            # Update plot
            t = np.arange(1, self.trajectory_length + 1)
            # label = self._create_label(unique)
            transform = unique["transform_name"]
            if transform == "MultitoSingleObjective":
                transform = "Custom"

            label = (
                f"""{unique["strategy_name"]} ({transform})"""
                if transform is not "Transform"
                else f"""{unique["strategy_name"]}"""
            )

            if include_experiment_ids:
                label += f" ({ids[0]}-{ids[-1]})"

            lower = hv_mean_trajectory - 1.96 * hv_std_trajectory
            lower = np.clip(lower, 0, None)
            upper = hv_mean_trajectory + 1.96 * hv_std_trajectory
            if plot_type == "matplotlib":
                ax.plot(t, hv_mean_trajectory, label=label, color=colors[c_num])
                ax.fill_between(t, lower, upper, alpha=0.1, color=colors[c_num])
            elif plot_type == "plotly":
                r, g, b = hex_to_rgb(colors[c_num])
                color = lambda alpha: f"rgba({r},{g},{b},{alpha})"
                fig.add_trace(
                    go.Scatter(
                        x=t,
                        y=hv_mean_trajectory,
                        mode="lines",
                        name=label,
                        line=dict(color=color(1)),
                        legendgroup=label,
                    )
                )
                fig.add_trace(
                    go.Scatter(
                        x=t,
                        y=lower,
                        mode="lines",
                        fill="tonexty",
                        line=dict(width=0),
                        fillcolor=color(0.1),
                        showlegend=False,
                        legendgroup=label,
                    )
                )
                fig.add_trace(
                    go.Scatter(
                        x=t,
                        y=upper,
                        mode="lines",
                        fill="tozeroy",
                        line=dict(width=0),
                        fillcolor=color(0.1),
                        showlegend=False,
                        legendgroup=label,
                    )
                )
            if cycle == c_num + 1:
                c_num = 0
            elif plot_type == "plotly":
                c_num += 1
            elif plot_type == "matplotlib":
                c_num += 2

        # Plot formattting
        if plot_type == "matplotlib":
            ax.set_xlabel("Experiments")
            ax.set_ylabel("Hypervolume")
            legend = ax.legend(loc="upper left")
            ax.tick_params(direction="in")
            ax.set_xlim(1, self.trajectory_length)
            if fig is None:
                return ax, legend
            else:
                return fig, ax, legend
        elif plot_type == "plotly":
            fig.update_layout(
                xaxis=dict(title="Experiments"), yaxis=dict(title="Hypervolume")
            )
            fig.show()
            return fig

    def _create_label(self, unique):
        transform_text = (
            unique["transform_name"]
            if unique["transform_name"] != "Transform"
            else "No transform"
        )
        chimera_params = f" (yld tol.={unique['yld_tolerance']}, cost tol.={unique['cost_tolerance']})"
        transform_text += (
            chimera_params if unique["transform_name"] == "Chimera" else ""
        )
        final_text = f"{unique['strategy_name']}, {transform_text}"
        # if unique["num_initial_experiments"] == 1:
        #     final_text = final_text.rstrip("s")
        return final_text

    def time_hv_bar_plot(self, ax=None):
        """Plot average CPU time and terminal hypervolume for hyperparameter combinations
        
        Parameters
        ----------
        ax : `matplotlib.pyplot.axes`, optional
            Matplotlib axis for plotting
        """
        df = self.df
        by = [
            "strategy_name",
            "transform_name",
            "yld_tolerance",
            "cost_tolerance",
            "use_descriptors",
            "batch_size",
        ]
        # Group repeats and take average.
        grouped_df = (
            df.groupby(by=by, dropna=False,)
            .head(self.num_repeats)
            .groupby(by=by, dropna=False)
        )

        # Mean and std deviation
        means = grouped_df.mean()
        stds = grouped_df.std()

        # Find the maximum terminal hypervolume for each strategy
        indices = means.groupby(by=["strategy_name"]).idxmax()["terminal_hypervolume"]
        means = means.loc[indices]
        stds = stds.loc[indices]

        # Ascending hypervolume
        ordered_indices = means["terminal_hypervolume"].argsort()
        means = means.iloc[ordered_indices]
        stds = stds.iloc[ordered_indices]

        # Convert to per iteration
        means["computation_t_iter"] = means["computation_t"] / self.trajectory_length
        stds["computation_t_iter"] = stds["computation_t"] / self.trajectory_length

        # Clip std deviations
        stds["terminal_hypervolume"] = stds["terminal_hypervolume"].clip(
            0, means["terminal_hypervolume"]
        )
        stds["computation_t_iter"] = stds["computation_t_iter"].clip(
            0, means["computation_t_iter"]
        )

        # Rename
        rename = {
            "terminal_hypervolume": "Terminal hypervolume",
            "computation_t_iter": "Computation time per iteration",
        }
        means = means.rename(columns=rename)
        stds = stds.rename(columns=rename)

        # Bar plot
        ax = means.plot(
            kind="bar",
            colormap=CMAP,
            y=["Terminal hypervolume", "Computation time per iteration"],
            secondary_y="Computation time per iteration",
            yerr=stds,
            capsize=4,
            mark_right=False,
            ax=ax,
            alpha=0.9,
        )
        strategy_names = means.index.to_frame()["strategy_name"].values
        transform_names = means.index.to_frame()["transform_name"].values
        for i, t in enumerate(transform_names):
            if t == "Transform":
                transform_names[i] = ""
            elif t == "MultitoSingleObjective":
                transform_names[i] = "Custom"
        ax.set_xticklabels(
            [
                f"{s} \n ({t})" if t is not "" else f"{s}"
                for s, t in zip(strategy_names, transform_names)
            ]
        )
        ax.set_xlabel("")
        ax.right_ax.set_ylabel("Time (seconds)")
        ax.right_ax.set_yscale("log")
        ax.set_ylabel("Hypervolume")
        plt.minorticks_off()
        for tick in ax.get_xticklabels():
            tick.set_rotation(45)

        return ax


def hex_to_rgb(hex_color: str) -> tuple:
    """Convert hex to RGA
    From https://community.plotly.com/t/scatter-plot-fill-with-color-how-to-set-opacity-of-fill/29591
    """
    hex_color = hex_color.lstrip("#")
    if len(hex_color) == 3:
        hex_color = hex_color * 2
    return int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
