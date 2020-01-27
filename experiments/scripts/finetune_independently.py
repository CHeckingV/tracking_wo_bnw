import pickle

from torchvision.models.detection.faster_rcnn import FastRCNNPredictor, TwoMLPHead

import numpy as np
from torch.nn import functional as F
from tracktor.frcnn_fpn import FRCNN_FPN
from tracktor.live_dataset import IndividualDataset
import time
import torch
from sacred import Experiment
import sklearn.metrics

from tracktor.visualization import VisdomLinePlotter

ex = Experiment()

ex.add_config('experiments/cfgs/tracktor.yaml')
ex.add_named_config('cfg1', 'experiments/cfgs/hp_search/cfg1.yaml')

device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")


def initialize_nets(obj_detect_weights):
    obj_detect = FRCNN_FPN(num_classes=2).to(device)
    obj_detect.load_state_dict(torch.load(obj_detect_weights,
                                              map_location=lambda storage, loc: storage))
    obj_detect.eval()

    bbox_predictor_weights = obj_detect.roi_heads.box_predictor.state_dict()
    bbox_head_weights = obj_detect.roi_heads.box_head.state_dict()

    box_predictor_classification = FastRCNNPredictor(1024, 2).to(device)
    box_predictor_classification.load_state_dict(bbox_predictor_weights)

    box_head_classification = TwoMLPHead(obj_detect.backbone.out_channels *
                                         obj_detect.roi_heads.box_roi_pool.output_size[0] ** 2,
                                         representation_size=1024).to(device)
    box_head_classification.load_state_dict(bbox_head_weights)
    return obj_detect, box_head_classification, box_predictor_classification

def do_finetuning(id, finetuning_config, plotter, box_head_classification, box_predictor_classification):
    dataset = pickle.load(open("training_set/feature_training_set_track_{}.pkl".format(id), "rb"))
    dataset.post_process()

    training_set, validation_set = dataset.val_test_split(num_frames_train=20, num_frames_val=10, train_val_frame_gap=5,
                                                          downsampling=True, shuffle=True)
    box_predictor_classification.train()
    box_head_classification.train()
    optimizer = torch.optim.Adam(
                list(box_predictor_classification.parameters()) + list(box_head_classification.parameters()), lr=float(finetuning_config["learning_rate"]) )
    scheduler = torch.optim.lr_scheduler.StepLR(optimizer, finetuning_config["decay_every"], gamma=finetuning_config['gamma'])
    train_dataloader = torch.utils.data.DataLoader(training_set, batch_size=512)
    val_dataloader = torch.utils.data.DataLoader(validation_set, batch_size=512)

    for i in range(int(finetuning_config["iterations"])):
        for i_sample, sample_batch in enumerate(train_dataloader):
            optimizer.zero_grad()
            loss = forward_pass_for_classifier_training(sample_batch['features'], sample_batch['scores'], box_head_classification, box_predictor_classification)
            loss.backward()
            optimizer.step()
            scheduler.step()
        plot_every = 2
        if np.mod(i, plot_every) == 0 and (finetuning_config["early_stopping_classifier"] or finetuning_config["plot_training_curves"]):

            positive_scores = forward_pass_for_classifier_training(
                sample_batch['features'][sample_batch['scores'] == 1], sample_batch['scores'], box_head_classification, box_predictor_classification, return_scores=True,
                eval=True)
            negative_scores = forward_pass_for_classifier_training(
                sample_batch['features'][sample_batch['scores'] == 0], sample_batch['scores'], box_head_classification, box_predictor_classification, return_scores=True,
                eval=True)

        if np.mod(i, plot_every) == 0 and finetuning_config["plot_training_curves"]:
            for sample_idx, score in enumerate(positive_scores):
                plotter.plot('score', 'positive {}'.format(sample_idx), 'Scores Evaluation Classifier for Track {}'.format(id),
                                  i, score.cpu().numpy(), is_target=True)
            for sample_idx, score in enumerate(negative_scores):
                plotter.plot('score', 'negative {}'.format(sample_idx), 'Scores Evaluation Classifier for Track {}'.format(id),
                                  i, score.cpu().numpy())

        if finetuning_config["early_stopping_classifier"] and torch.min(positive_scores) - torch.max(negative_scores) > 1.5:
            break

        if np.mod(i, plot_every) == 0 and finetuning_config["validate"]:
            for val_batch_idx, val_batch in enumerate(val_dataloader):
                if finetuning_config["validate"]:
                    val_positive_scores = forward_pass_for_classifier_training(
                        val_batch['features'][val_batch['scores'] == 1], val_batch['scores'], box_head_classification, box_predictor_classification, return_scores=True,
                        eval=True)
                    val_negative_scores = forward_pass_for_classifier_training(
                        val_batch['features'][val_batch['scores'] == 0], val_batch['scores'], box_head_classification, box_predictor_classification, return_scores=True,
                        eval=True)

                if finetuning_config["validate"]:
                    for sample_idx, score in enumerate(val_positive_scores):
                        plotter.plot('score', 'val positive {}'.format(sample_idx), 'Scores Evaluation Classifier for Track {}'.format(id),
                                          i, score.cpu().numpy(), is_val_target=True)
                    for sample_idx, score in enumerate(val_negative_scores):
                        plotter.plot('score', 'val negative {}'.format(sample_idx), 'Scores Evaluation Classifier for Track {}'.format(id),
                                          i, score.cpu().numpy(), is_val_pred=True)


        box_predictor_classification.eval()
        box_head_classification.eval()

    total_samples = 0
    loss = 0
    true_labels = torch.tensor([])
    predicted_labels = torch.tensor([])

    for idx, batch in enumerate(val_dataloader):
        new_true_scores = batch['scores'].to('cpu')
        true_labels = torch.cat([true_labels, new_true_scores])
        predicted_scores = forward_pass_for_classifier_training(
                batch['features'], batch['scores'], box_head_classification, box_predictor_classification, return_scores=True,
                eval=True)
        new_predicted_labels = predicted_scores
        new_predicted_labels[predicted_scores > 0.5] = 1
        new_predicted_labels[predicted_scores < 0.5] = 0
        predicted_labels = torch.cat([predicted_labels, new_predicted_labels.to('cpu')])
        loss += forward_pass_for_classifier_training(batch['features'], batch['scores'], box_head_classification, box_predictor_classification)
        total_samples += batch['features'].size()[0]

        print('Loss for track {}: {}'.format(id, loss / total_samples))
        f1_score = sklearn.metrics.f1_score(true_labels, predicted_labels)
        print('F1 Score for track {}: {}'.format(id, f1_score))
    return f1_score

def forward_pass_for_classifier_training(features, scores, box_head_classification, box_predictor_classification, eval=False, return_scores=False):
    if eval:
        box_predictor_classification.eval()
        box_head_classification.eval()
    feat = box_head_classification(features)
    class_logits, _ = box_predictor_classification(feat)
    if return_scores:
        pred_scores = F.softmax(class_logits, -1)
        if eval:
            box_predictor_classification.train()
            box_head_classification.train()
        return pred_scores[:, 1:].squeeze(dim=1).detach()
    loss = F.cross_entropy(class_logits, scores.long())
    if eval:
        box_predictor_classification.train()
        box_head_classification.train()
    return loss

@ex.automain
def main(tracktor, _config, _log, _run):

    tracker_cfg = tracktor['tracker']
    finetuning_config = tracker_cfg['finetuning']
    obj_detect_weights = _config['tracktor']['obj_detect_model']
    f1_scores = []

    track_ids = [6, 24, 13, 25, 27]
    for track_id in track_ids:
        if finetuning_config['validate'] or finetuning_config['plot_training_curves']:
            plotter = VisdomLinePlotter(env_name='finetune_independently')
        else:
            plotter = None
        obj_detect, box_head_classification, box_predictor_classification = initialize_nets(obj_detect_weights)

        try:
            f1_score = do_finetuning(track_id, finetuning_config, plotter, box_head_classification, box_predictor_classification)
            f1_scores.append(f1_score)
        except AssertionError:
            continue
        print(np.mean(f1_scores))
    print(np.sum(np.array(f1_scores)>0.99))
    print(f1_scores)
