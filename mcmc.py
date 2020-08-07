"""MCMC Test Rig for COVID-19 UK model"""
import optparse
import os
import pickle as pkl
from collections import OrderedDict
from time import perf_counter

import h5py
import numpy as np
import tensorflow as tf
import tensorflow_probability as tfp
import tqdm
import yaml

from covid import config
from covid.model import load_data, CovidUKStochastic
from covid.pydata import phe_case_data
from covid.util import sanitise_parameter, sanitise_settings, impute_previous_cases
from covid.impl.mcmc import UncalibratedLogRandomWalk, random_walk_mvnorm_fn
from covid.impl.event_time_mh import UncalibratedEventTimesUpdate
from covid.impl.occult_events_mh import UncalibratedOccultUpdate
from covid.impl.gibbs import DeterministicScanKernel, GibbsStep, flatten_results
from covid.impl.multi_scan_kernel import MultiScanKernel

###########
# TF Bits #
###########

tfd = tfp.distributions
tfb = tfp.bijectors

DTYPE = config.floatX

# os.environ["CUDA_VISIBLE_DEVICES"] = "-1"
# os.environ["XLA_FLAGS"] = '--xla_dump_to=xla_dump --xla_dump_hlo_pass_re=".*"'

if tf.test.gpu_device_name():
    print("Using GPU")
else:
    print("Using CPU")

# Read in settings
parser = optparse.OptionParser()
parser.add_option(
    "--config",
    "-c",
    dest="config",
    default="example_config.yaml",
    help="configuration file",
)
options, cmd_args = parser.parse_args()
print("Loading config file:", options.config)

with open(options.config, "r") as f:
    config = yaml.load(f, Loader=yaml.FullLoader)

settings = sanitise_settings(config["settings"])

param = sanitise_parameter(config["parameter"])
param = {k: tf.constant(v, dtype=DTYPE) for k, v in param.items()}

covar_data = load_data(config["data"], settings, DTYPE)

cases = phe_case_data(config["data"]["reported_cases"], settings["inference_period"])
ei_events, lag_ei = impute_previous_cases(cases, 0.44)
se_events, lag_se = impute_previous_cases(ei_events, 2.0)
ir_events = np.pad(cases, ((0, 0), (lag_ei + lag_se - 2, 0)))
ei_events = np.pad(ei_events, ((0, 0), (lag_se - 1, 0)))


model = CovidUKStochastic(
    C=covar_data["C"],
    N=covar_data["pop"],
    W=covar_data["W"],
    date_range=settings["inference_period"],
    holidays=settings["holiday"],
    xi_freq=14,
    time_step=1.0,
)

##########################
# Log p and MCMC kernels #
##########################
def logp(theta, xi, events, occult_events):
    p = param
    p["beta1"] = tf.convert_to_tensor(theta[0], dtype=DTYPE)
    p["beta2"] = tf.convert_to_tensor(theta[1], dtype=DTYPE)
    p["gamma"] = tf.convert_to_tensor(theta[2], dtype=DTYPE)
    p["xi"] = tf.convert_to_tensor(xi, dtype=DTYPE)

    beta1_logp = tfd.Gamma(
        concentration=tf.constant(1.0, dtype=DTYPE), rate=tf.constant(1.0, dtype=DTYPE)
    ).log_prob(p["beta1"])

    sigma = tf.constant(0.1, dtype=DTYPE)
    phi = tf.constant(12.0, dtype=DTYPE)
    kernel = tfp.math.psd_kernels.MaternThreeHalves(sigma, phi)
    xi_logp = tfd.GaussianProcess(
        kernel, index_points=tf.cast(model.xi_times[:, tf.newaxis], DTYPE)
    ).log_prob(p["xi"])

    spatial_beta_logp = tfd.Gamma(
        concentration=tf.constant(3.0, dtype=DTYPE), rate=tf.constant(10.0, dtype=DTYPE)
    ).log_prob(p["beta2"])

    gamma_logp = tfd.Gamma(
        concentration=tf.constant(100.0, dtype=DTYPE),
        rate=tf.constant(400.0, dtype=DTYPE),
    ).log_prob(p["gamma"])
    with tf.name_scope("epidemic_log_posterior"):
        y_logp = model.log_prob(events + occult_events, p, state_init)
    logp = beta1_logp + spatial_beta_logp + gamma_logp + xi_logp + y_logp
    return logp


# Pavel's suggestion for a Gibbs kernel requires
# kernel factory functions.
def make_theta_kernel(scale, bounded_convergence):
    return GibbsStep(
        0,
        tfp.mcmc.MetropolisHastings(
            inner_kernel=UncalibratedLogRandomWalk(
                target_log_prob_fn=logp,
                new_state_fn=random_walk_mvnorm_fn(scale, p_u=bounded_convergence),
            )
        ),
        name="update_theta",
    )


def make_xi_kernel(scale, bounded_convergence):
    return GibbsStep(
        1,
        tfp.mcmc.RandomWalkMetropolis(
            target_log_prob_fn=logp,
            new_state_fn=random_walk_mvnorm_fn(scale, p_u=bounded_convergence),
        ),
        name="xi_update",
    )


def make_events_step(target_event_id, prev_event_id=None, next_event_id=None):
    return GibbsStep(
        2,
        tfp.mcmc.MetropolisHastings(
            inner_kernel=UncalibratedEventTimesUpdate(
                target_log_prob_fn=logp,
                target_event_id=target_event_id,
                prev_event_id=prev_event_id,
                next_event_id=next_event_id,
                initial_state=state_init,
                dmax=config["mcmc"]["dmax"],
                mmax=config["mcmc"]["m"],
                nmax=config["mcmc"]["nmax"],
            )
        ),
        name="event_update",
    )


def make_occults_step(target_event_id):
    return GibbsStep(
        3,
        tfp.mcmc.MetropolisHastings(
            inner_kernel=UncalibratedOccultUpdate(
                target_log_prob_fn=logp,
                target_event_id=target_event_id,
                nmax=config["mcmc"]["occult_nmax"],
                t_range=(se_events.shape[1] - 22, se_events.shape[1] - 1),
            )
        ),
        name="occult_update",
    )


def is_accepted(result):
    if hasattr(result, "is_accepted"):
        return tf.cast(result.is_accepted, DTYPE)
    return is_accepted(result.inner_results)


def trace_results_fn(_, results):
    """Returns log_prob, accepted, q_ratio"""

    def f(result):
        log_prob = result.proposed_results.target_log_prob
        accepted = is_accepted(result)
        q_ratio = result.proposed_results.log_acceptance_correction
        if hasattr(result.proposed_results, "extra"):
            proposed = tf.cast(result.proposed_results.extra, log_prob.dtype)
            return tf.concat([[log_prob], [accepted], [q_ratio], proposed], axis=0)
        return tf.concat([[log_prob], [accepted], [q_ratio]], axis=0)

    def recurse(f, list_or_atom):
        if isinstance(list_or_atom, list):
            return [recurse(f, x) for x in list_or_atom]
        return f(list_or_atom)

    return recurse(f, results)


@tf.function(autograph=False, experimental_compile=True)
def sample(n_samples, init_state, scale_theta, scale_xi, num_event_updates):
    with tf.name_scope("main_mcmc_sample_loop"):

        init_state = init_state.copy()

        kernel = DeterministicScanKernel(
            [
                make_theta_kernel(theta_scale, 0.0),
                make_xi_kernel(xi_scale, 0.0),
                MultiScanKernel(
                    config["mcmc"]["num_event_time_updates"],
                    DeterministicScanKernel(
                        [
                            make_events_step(0, None, 1),
                            make_events_step(1, 0, 2),
                            make_occults_step(0),
                            make_occults_step(1),
                        ]
                    ),
                ),
            ],
            name="gibbs_kernel",
        )

        samples, results = tfp.mcmc.sample_chain(
            n_samples, init_state, kernel=kernel, trace_fn=trace_results_fn
        )

        return samples, results


##################
# MCMC loop here #
##################

# MCMC Control
NUM_BURSTS = config["mcmc"]["num_bursts"]
NUM_BURST_SAMPLES = config["mcmc"]["num_burst_samples"]
NUM_EVENT_TIME_UPDATES = config["mcmc"]["num_event_time_updates"]
THIN_BURST_SAMPLES = NUM_BURST_SAMPLES // config["mcmc"]["thin"]
NUM_SAVED_SAMPLES = THIN_BURST_SAMPLES * NUM_BURSTS

# RNG stuff
tf.random.set_seed(2)

# Initial state.  NB [M, T, X] layout for events.
events = tf.stack([se_events, ei_events, ir_events], axis=-1)
state_init = tf.concat([model.N[:, tf.newaxis], events[:, 0, :]], axis=-1)
events = events[:, 1:, :]
current_state = [
    np.array([0.85, 0.3, 0.25], dtype=DTYPE),
    np.zeros(model.num_xi, dtype=DTYPE),
    events,
    tf.zeros_like(events),
]

# Output Files
posterior = h5py.File(
    os.path.expandvars(config["output"]["posterior"]),
    "w",
    rdcc_nbytes=1024 ** 2 * 400,
    rdcc_nslots=100000,
)
event_size = [NUM_SAVED_SAMPLES] + list(current_state[2].shape)

theta_samples = posterior.create_dataset(
    "samples/theta", [NUM_SAVED_SAMPLES, current_state[0].shape[0]], dtype=np.float64,
)
xi_samples = posterior.create_dataset(
    "samples/xi", [NUM_SAVED_SAMPLES, current_state[1].shape[0]], dtype=np.float64,
)
event_samples = posterior.create_dataset(
    "samples/events",
    event_size,
    dtype=DTYPE,
    chunks=(32, 64, 64, 1),
    compression="szip",
    compression_opts=("nn", 16),
)
occult_samples = posterior.create_dataset(
    "samples/occults",
    event_size,
    dtype=DTYPE,
    chunks=(32, 64, 64, 1),
    compression="szip",
    compression_opts=("nn", 16),
)

output_results = [
    posterior.create_dataset("results/theta", (NUM_SAVED_SAMPLES, 3), dtype=DTYPE,),
    posterior.create_dataset("results/xi", (NUM_SAVED_SAMPLES, 3), dtype=DTYPE,),
    posterior.create_dataset(
        "results/move/S->E", (NUM_SAVED_SAMPLES, 3 + model.N.shape[0]), dtype=DTYPE,
    ),
    posterior.create_dataset(
        "results/move/E->I", (NUM_SAVED_SAMPLES, 3 + model.N.shape[0]), dtype=DTYPE,
    ),
    posterior.create_dataset(
        "results/occult/S->E", (NUM_SAVED_SAMPLES, 6), dtype=DTYPE
    ),
    posterior.create_dataset(
        "results/occult/E->I", (NUM_SAVED_SAMPLES, 6), dtype=DTYPE
    ),
]

print("Initial logpi:", logp(*current_state))

theta_scale = tf.constant(
    [[0.1, 0.0, 0.0], [0.0, 0.8, 0.0], [0.0, 0.0, 0.1]], dtype=current_state[0].dtype
)
xi_scale = tf.linalg.diag(
    tf.constant([0.1] * model.num_xi.numpy(), dtype=current_state[1].dtype)
)

# We loop over successive calls to sample because we have to dump results
#   to disc, or else end OOM (even on a 32GB system).
# with tf.profiler.experimental.Profile("/tmp/tf_logdir"):
for i in tqdm.tqdm(range(NUM_BURSTS), unit_scale=NUM_BURST_SAMPLES):
    samples, results = sample(
        NUM_BURST_SAMPLES,
        init_state=current_state,
        scale_theta=theta_scale,
        scale_xi=xi_scale,
        num_event_updates=tf.constant(NUM_EVENT_TIME_UPDATES, tf.int32),
    )
    current_state = [s[-1] for s in samples]
    s = slice(i * THIN_BURST_SAMPLES, i * THIN_BURST_SAMPLES + THIN_BURST_SAMPLES)
    idx = tf.constant(range(0, NUM_BURST_SAMPLES, config["mcmc"]["thin"]))
    theta_samples[s, ...] = tf.gather(samples[0], idx)
    xi_samples[s, ...] = tf.gather(samples[1], idx)
    cov = np.cov(
        np.log(theta_samples[: (i * NUM_BURST_SAMPLES + NUM_BURST_SAMPLES), ...]),
        rowvar=False,
    )
    print(current_state[0].numpy(), flush=True)
    print(cov, flush=True)
    if (i * NUM_BURST_SAMPLES) > 1000 and np.all(np.isfinite(cov)):
        theta_scale = 2.38 ** 2 * cov / 2.0

    start = perf_counter()
    event_samples[s, ...] = tf.gather(samples[2], idx)
    occult_samples[s, ...] = tf.gather(samples[3], idx)
    end = perf_counter()

    flat_results = flatten_results(results)
    for i, ro in enumerate(output_results):
        ro[s, ...] = tf.gather(flat_results[i], idx)

    print("Storage time:", end - start, "seconds")
    print(
        "Acceptance theta:", tf.reduce_mean(tf.cast(flat_results[0][:, 1], tf.float32))
    )
    print("Acceptance xi:", tf.reduce_mean(tf.cast(flat_results[1][:, 1], tf.float32)))
    print(
        "Acceptance move S->E:",
        tf.reduce_mean(tf.cast(flat_results[2][:, 1], tf.float32)),
    )
    print(
        "Acceptance move E->I:",
        tf.reduce_mean(tf.cast(flat_results[3][:, 1], tf.float32)),
    )
    print(
        "Acceptance occult S->E:",
        tf.reduce_mean(tf.cast(flat_results[4][:, 1], tf.float32)),
    )
    print(
        "Acceptance occult E->I:",
        tf.reduce_mean(tf.cast(flat_results[5][:, 1], tf.float32)),
    )

print(f"Acceptance param: {output_results[0][:, 1].mean()}")
print(f"Acceptance move S->E: {output_results[1][:, 1].mean()}")
print(f"Acceptance move E->I: {output_results[2][:, 1].mean()}")
print(f"Acceptance occult S->E: {output_results[3][:, 1].mean()}")
print(f"Acceptance occult E->I: {output_results[4][:, 1].mean()}")

posterior.close()
