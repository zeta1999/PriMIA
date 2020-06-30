import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import syft as sy
import configparser
import argparse
import visdom
import tqdm
import shutil
import random
import numpy as np
import multiprocessing as mp
from os import path, remove
from warnings import warn
from datetime import datetime
from tabulate import tabulate
from torchvision import datasets, transforms, models
from torchlib.dataloader import PPPP, LabelMNIST  # pylint:disable=import-error
from torchlib.models import (  # pylint:disable=import-error
    vgg16,
    resnet18,
    conv_at_resolution,
)
from torchlib.utils import (  # pylint:disable=import-error
    LearningRateScheduler,
    Arguments,
    train,
    train_federated,
    test,
    save_model,
    save_config_results,
    AddGaussianNoise,
)
import kornia.augmentation as aug


def calc_class_weights(args, train_loader):
    comparison = list(
        torch.split(
            torch.zeros((num_classes, args.batch_size)), 1  # pylint:disable=no-member
        )
    )
    for i in range(num_classes):
        comparison[i] += i
    occurances = torch.zeros(num_classes)  # pylint:disable=no-member
    for worker, tl in tqdm.tqdm(
        train_loader.items(),
        total=len(train_loader),
        leave=False,
        desc="calc class weights",
    ):
        if args.train_federated:
            for i in range(num_classes):
                comparison[i] = comparison[i].send(worker.id)
        for _, target in tqdm.tqdm(
            tl,
            leave=False,
            desc="calc class weights on {:s}".format(worker.id),
            total=len(tl),
        ):
            if target.shape[0] < args.batch_size:
                # the last batch is not considered
                # TODO: find solution for this
                continue
            for i in range(num_classes):
                n = target.eq(comparison[i]).sum().copy()
                if args.train_federated:
                    n = n.get()
                occurances[i] += n.item()
        if args.train_federated:
            for i in range(num_classes):
                comparison[i] = comparison[i].get()
    if torch.sum(occurances).item() == 0:  # pylint:disable=no-member
        warn("class weights could not be calculated - no weights are used")
        return torch.ones((num_classes,))  # pylint:disable=no-member
    cw = 1.0 / occurances
    cw /= torch.sum(cw)  # pylint:disable=no-member
    return cw


def setup_pysyft(args, verbose=False):
    from torchlib.websocket_utils import (  # pylint:disable=import-error
        read_websocket_config,
    )

    # torch.set_num_threads(1)  # pylint:disable=no-member
    # hook.local_worker.is_client_worker = False
    # server = hook.local_worker
    worker_dict = read_websocket_config("configs/websetting/config.csv")
    worker_names = [id_dict["id"] for _, id_dict in worker_dict.items()]
    """if "validation" in worker_names:
        worker_names.remove("validation")"""

    if args.websockets:
        workers = {
            worker["id"]: sy.workers.node_client.NodeClient(
                hook,
                "http://{:s}:{:s}".format(worker["host"], worker["port"]),
                id=worker["id"],
                verbose=verbose,
            )
            for _, worker in worker_dict.items()
        }

    else:
        workers = {
            worker["id"]: sy.VirtualWorker(hook, id=worker["id"], verbose=verbose)
            for _, worker in worker_dict.items()
        }
        train_loader = None
        for i, worker in tqdm.tqdm(
            enumerate(workers.values()),
            total=len(workers.keys()),
            leave=False,
            desc="load data",
        ):
            if args.dataset == "mnist":
                node_id = worker.id
                KEEP_LABELS_DICT = {
                    "alice": [0, 1, 2, 3],
                    "bob": [4, 5, 6],
                    "charlie": [7, 8, 9],
                    None: list(range(10)),
                }
                dataset = LabelMNIST(
                    labels=KEEP_LABELS_DICT[node_id]
                    if node_id in KEEP_LABELS_DICT
                    else KEEP_LABELS_DICT[None],
                    root="./data",
                    train=True,
                    download=True,
                    transform=transforms.Compose(
                        [
                            transforms.ToTensor(),
                            transforms.Normalize((0.1307,), (0.3081,)),
                        ]
                    ),
                )
            elif args.dataset == "pneumonia":
                if worker.id == "validation":
                    train_tf = [
                        transforms.Resize(args.inference_resolution),
                        transforms.CenterCrop(args.train_resolution),
                        transforms.ToTensor(),
                        transforms.Normalize((0.57282609,), (0.17427578,)),
                    ]
                else:
                    train_tf = [
                        transforms.RandomVerticalFlip(p=args.vertical_flip_prob),
                        transforms.RandomAffine(
                            degrees=args.rotation,
                            translate=(args.translate, args.translate),
                            scale=(1.0 - args.scale, 1.0 + args.scale),
                            shear=args.shear,
                            #    fillcolor=0,
                        ),
                        transforms.Resize(args.inference_resolution),
                        transforms.RandomCrop(args.train_resolution),
                        transforms.ToTensor(),
                        transforms.Normalize((0.57282609,), (0.17427578,)),
                        AddGaussianNoise(
                            mean=0.0, std=args.noise_std, p=args.noise_prob
                        ),
                    ]
                target_dict_pneumonia = {0: 1, 1: 0, 2: 2}
                dataset = datasets.ImageFolder(
                    # path.join("data/server_simulation/", "validation")
                    # if worker.id == "validation"
                    # else
                    path.join("data/server_simulation/", "worker{:d}".format(i + 1)),
                    transform=transforms.Compose(train_tf),
                    target_transform=lambda x: target_dict_pneumonia[x],
                )

            else:
                raise NotImplementedError(
                    "federation for virtual workers for this dataset unknown"
                )
            data, targets = [], []
            repetitions = (  # 1 if worker.id == "validation" else
                args.repetitions_dataset
            )
            for j in tqdm.tqdm(
                range(repetitions),
                total=repetitions,
                leave=False,
                desc="register data on {:s}".format(worker.id),
            ):
                for d, t in tqdm.tqdm(
                    dataset,
                    total=len(dataset),
                    leave=False,
                    desc="register data {:d}. time".format(j + 1),
                ):
                    data.append(d)
                    targets.append(t)
            selected_data = torch.stack(data)  # pylint:disable=no-member
            selected_targets = torch.tensor(targets)  # pylint:disable=not-callable
            del data, targets
            selected_data.tag(
                args.dataset,  # "#valdata" if worker.id == "validation" else
                "#traindata",
            )
            selected_targets.tag(
                args.dataset,
                # "#valtargets" if worker.id == "validation" else
                "#traintargets",
            )
            worker.load_data([selected_data, selected_targets])

    grid: sy.PrivateGridNetwork = sy.PrivateGridNetwork(*workers.values())
    data = grid.search(args.dataset, "#traindata")
    target = grid.search(args.dataset, "#traintargets")
    train_loader = {}
    total_L = 0
    for worker in data.keys():
        dist_dataset = [  # TODO: in the future transform here would be nice but currently raise errors
            sy.BaseDataset(
                data[worker][0], target[worker][0],
            )  # transform=federated_tf
        ]
        fed_dataset = sy.FederatedDataset(dist_dataset)
        total_L += len(fed_dataset)
        tl = sy.FederatedDataLoader(
            fed_dataset, batch_size=args.batch_size, shuffle=True
        )
        train_loader[workers[worker]] = tl

    """data = grid.search(args.dataset, "#valdata")
    target = grid.search(args.dataset, "#valtargets")
    if "validation" in data.keys():
        val_set = sy.FederatedDataset(
            [sy.BaseDataset(data["validation"][0], target["validation"][0],)]
        )
        val_loader = (
            workers["validation"],
            sy.FederatedDataLoader(
                val_set, batch_size=args.test_batch_size, shuffle=False
            ),
        )
        print("Found {:d} validation images".format(len(val_set)))"""
    if args.dataset == "mnist":
        valset = LabelMNIST(
            labels=list(range(10)),
            root="./data",
            train=False,
            download=True,
            transform=transforms.Compose(
                [transforms.ToTensor(), transforms.Normalize((0.1307,), (0.3081,)),]
            ),
        )
    elif args.dataset == "pneumonia":
        val_tf = [
            transforms.Resize(args.inference_resolution),
            transforms.CenterCrop(args.train_resolution),
            transforms.ToTensor(),
            transforms.Normalize((0.57282609,), (0.17427578,)),
        ]
        target_dict_pneumonia = {0: 1, 1: 0, 2: 2}
        valset = datasets.ImageFolder(
            path.join("data/server_simulation/", "validation"),
            transform=transforms.Compose(val_tf),
            target_transform=lambda x: target_dict_pneumonia[x],
        )
    val_loader = torch.utils.data.DataLoader(
        valset, batch_size=args.test_batch_size, shuffle=False
    )
    assert len(train_loader.keys()) == (
        # len(workers.keys()) - 1 if "validation" in workers.keys() else
        len(workers.keys())
    ), "data was not correctly loaded"
    print("Found a total dataset with {:d} samples on remote workers".format(total_L))
    print(
        "Found a total validation set with {:d} samples on remote workers".format(
            len(val_loader.dataset)
        )
    )
    return train_loader, val_loader, total_L, workers, worker_names


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config", type=str, required=True, help="Path to config",
    )
    parser.add_argument(
        "--train_federated", action="store_true", help="Train in federated setting"
    )
    parser.add_argument(
        "--dataset",
        type=str,
        default="pneumonia",
        choices=["pneumonia", "mnist"],
        required=True,
        help="which dataset?",
    )
    parser.add_argument(
        "--no_visdom", action="store_false", help="dont use a visdom server"
    )
    parser.add_argument("--no_cuda", action="store_true", help="dont use gpu")
    parser.add_argument(
        "--resume_checkpoint",
        type=str,
        default=None,
        help="Start training from older model checkpoint",
    )
    parser.add_argument(
        "--websockets", action="store_true", help="train on websocket config"
    )
    parser.add_argument(
        "--verbose", action="store_true", help="set syft workers to verbose"
    )
    cmd_args = parser.parse_args()

    config = configparser.ConfigParser()
    assert path.isfile(cmd_args.config), "config file not found"
    config.read(cmd_args.config)

    args = Arguments(cmd_args, config, mode="train")
    if args.websockets:
        assert args.train_federated, "Websockets only work when it is federated"
    print(str(args))

    use_cuda = not args.no_cuda and torch.cuda.is_available()

    torch.manual_seed(args.seed)
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False

    device = torch.device("cuda" if use_cuda else "cpu")  # pylint: disable=no-member

    kwargs = {"num_workers": 1, "pin_memory": True} if use_cuda else {}

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
    exp_name = "{:s}_{:s}_{:s}".format(
        "federated" if args.train_federated else "vanilla", args.dataset, timestamp
    )

    """
    Dataset creation and definition
    """
    dataset_classes = {"mnist": 10, "pneumonia": 3}
    num_classes = dataset_classes[args.dataset]
    class_name_dict = {
        "pneumonia": {0: "normal", 1: "bacterial pneumonia", 2: "viral pneumonia"}
    }
    class_names = (
        class_name_dict[args.dataset]
        if args.dataset in class_name_dict.keys()
        else None
    )
    if args.train_federated:

        hook = sy.TorchHook(torch)
        train_loader, val_loader, total_L, workers, worker_names = setup_pysyft(
            args, verbose=cmd_args.verbose
        )
    else:
        if args.dataset == "mnist":
            train_tf = [
                transforms.Resize(args.train_resolution),
                transforms.ToTensor(),
                transforms.Normalize((0.1307,), (0.3081,)),
            ]
            test_tf = [
                transforms.Resize(args.inference_resolution),
                transforms.ToTensor(),
                transforms.Normalize((0.1307,), (0.3081,)),
            ]
            if args.pretrained:
                repeat = transforms.Lambda(
                    lambda x: torch.repeat_interleave(  # pylint: disable=no-member
                        x, 3, dim=0
                    )
                )
                train_tf.append(repeat)
                test_tf.append(repeat)
            dataset = datasets.MNIST(
                "../data",
                train=True,
                download=True,
                transform=transforms.Compose(train_tf),
            )

        elif args.dataset == "pneumonia":
            """
            Different train and inference resolution only works with adaptive
            pooling in model activated
            """

            train_tf = [
                transforms.RandomVerticalFlip(p=args.vertical_flip_prob),
                transforms.RandomAffine(
                    degrees=args.rotation,
                    translate=(args.translate, args.translate),
                    scale=(1.0 - args.scale, 1.0 + args.scale),
                    shear=args.shear,
                    #    fillcolor=0,
                ),
                transforms.Resize(args.inference_resolution),
                transforms.RandomCrop(args.train_resolution),
                transforms.ToTensor(),
                transforms.Normalize((0.57282609,), (0.17427578,)),
                AddGaussianNoise(mean=0.0, std=args.noise_std, p=args.noise_prob),
            ]
            test_tf = [
                transforms.Resize(args.inference_resolution),
                transforms.CenterCrop(args.inference_resolution),
                transforms.ToTensor(),
                transforms.Normalize((0.57282609,), (0.17427578,)),
            ]

            """
            Duplicate grayscale one channel image into 3 channels
            """
            if args.pretrained:
                repeat = transforms.Lambda(
                    lambda x: torch.repeat_interleave(  # pylint: disable=no-member
                        x, 3, dim=0
                    )
                )
                train_tf.append(repeat)
                test_tf.append(repeat)

            dataset = PPPP(
                "data/Labels.csv",
                train=True,
                transform=transforms.Compose(train_tf),
                seed=args.seed,
            )

            occurances = dataset.get_class_occurances()

        else:
            raise NotImplementedError("dataset not implemented")

        total_L = total_L if args.train_federated else len(dataset)
        fraction = 1.0 / args.val_split
        dataset, valset = torch.utils.data.random_split(
            dataset,
            [int(round(total_L * (1.0 - fraction))), int(round(total_L * fraction))],
        )
        train_loader = torch.utils.data.DataLoader(
            dataset, batch_size=args.batch_size, shuffle=True, **kwargs
        )
        val_loader = torch.utils.data.DataLoader(
            valset, batch_size=args.test_batch_size, shuffle=False, **kwargs,
        )
        del total_L, fraction

    cw = None
    if args.class_weights:
        if "occurances" in locals():
            cw = torch.zeros((len(occurances)))  # pylint: disable=no-member
            for c, n in occurances.items():
                cw[c] = 1.0 / float(n)
            cw /= torch.sum(cw)  # pylint: disable=no-member
        else:
            cw = calc_class_weights(args, train_loader)
        cw = cw.to(device)

    scheduler = LearningRateScheduler(
        args.epochs, np.log10(args.lr), np.log10(args.end_lr), restarts=args.restarts
    )

    ## visdom
    vis_params = None
    if args.visdom:
        vis = visdom.Visdom()
        assert vis.check_connection(
            timeout_seconds=3
        ), "No connection could be formed quickly"
        vis_env = path.join(
            args.dataset, "federated" if args.train_federated else "vanilla", timestamp
        )
        plt_dict = dict(
            name="training loss",
            ytickmax=10,
            xlabel="epoch",
            ylabel="loss",
            legend=["train_loss"],
        )
        vis.line(
            X=np.zeros((1, 3)),
            Y=np.zeros((1, 3)),
            win="loss_win",
            opts={
                "legend": ["train_loss", "val_loss", "accuracy"],
                "xlabel": "epochs",
                "ylabel": "loss / accuracy [%]",
            },
            env=vis_env,
        )
        vis.line(
            X=np.zeros((1, 1)),
            Y=np.zeros((1, 1)),
            win="lr_win",
            opts={"legend": ["learning_rate"], "xlabel": "epochs", "ylabel": "lr"},
            env=vis_env,
        )
        vis_params = {"vis": vis, "vis_env": vis_env}
    # model = Net().to(device)
    if args.model == "vgg16":
        model = vgg16(
            pretrained=args.pretrained,
            num_classes=num_classes,
            in_channels=3 if args.dataset == "pneumonia" else 1,
            adptpool=False,
            input_size=args.inference_resolution,
        )
    elif args.model == "simpleconv":
        if args.pretrained:
            warn("No pretrained version available")

        model = conv_at_resolution[args.train_resolution](
            num_classes=num_classes, in_channels=3 if args.dataset == "pneumonia" else 1
        )
        """if args.train_federated:
            
            data_shape = torch.ones(  # pylint: disable=no-member
                (
                    args.batch_size,
                    3 if args.dataset == "pneumonia" else 1,
                    args.train_resolution,
                    args.train_resolution,
                ),
                device=device,
            )
            print(data_shape.size())
            model.build(data_shape)"""
    elif args.model == "resnet-18":
        model = resnet18(
            pretrained=args.pretrained,
            num_classes=num_classes,
            in_channels=3 if args.dataset == "pneumonia" else 1,
            adptpool=False,
            input_size=args.inference_resolution,
        )
    else:
        raise NotImplementedError("model unknown")

    # model = resnet18(pretrained=False, num_classes=num_classes, in_channels=1)
    # model = models.vgg16(pretrained=False, num_classes=3)
    # model.classifier = vggclassifier()
    if args.optimizer == "SGD":
        optimizer = optim.SGD(
            model.parameters(), lr=args.lr, weight_decay=args.weight_decay,
        )  # TODO momentum is not supported at the moment
    elif args.optimizer == "Adam":
        optimizer = optim.Adam(
            model.parameters(),
            lr=args.lr,
            betas=(args.beta1, args.beta2),
            weight_decay=args.weight_decay,
        )
    else:
        raise NotImplementedError("optimization not implemented")
    if args.train_federated:
        from syft.federated.floptimizer import Optims

        optimizer = Optims(worker_names, optimizer)
    loss_fn = nn.CrossEntropyLoss(weight=cw, reduction="mean")

    start_at_epoch = 1
    if cmd_args.resume_checkpoint:
        print("resuming checkpoint - args will be overwritten")
        state = torch.load(cmd_args.resume_checkpoint, map_location=device)
        start_at_epoch = state["epoch"]
        args = state["args"]
        if cmd_args.train_federated and args.train_federated:
            opt_state_dict = state["optim_state_dict"]
            for w in worker_names:
                optimizer.get_optim(w).load_state_dict(opt_state_dict[w])
        elif not cmd_args.train_federated and not args.train_federated:
            optimizer.load_state_dict(state["optim_state_dict"])
        else:
            pass  # not possible to load previous optimizer if setting changed
        args.incorporate_cmd_args(cmd_args)
        model.load_state_dict(state["model_state_dict"])
    model.to(device)

    test(
        args,
        model,
        device,
        val_loader,
        start_at_epoch - 1,
        loss_fn,
        num_classes,
        vis_params=vis_params,
        class_names=class_names,
    )
    accuracies = []
    model_paths = []
    for epoch in range(start_at_epoch, args.epochs + 1):
        if args.train_federated:
            for w in worker_names:
                new_lr = scheduler.adjust_learning_rate(
                    optimizer.get_optim(w), epoch - 1
                )
        else:
            new_lr = scheduler.adjust_learning_rate(optimizer, epoch - 1)
        if args.visdom:
            vis.line(
                X=np.asarray([epoch - 1]),
                Y=np.asarray([new_lr]),
                win="lr_win",
                name="learning_rate",
                update="append",
                env=vis_env,
            )

        try:
            if args.train_federated:
                model = train_federated(
                    args,
                    model,
                    device,
                    train_loader,
                    optimizer,
                    epoch,
                    loss_fn,
                    vis_params=vis_params,
                )

            else:
                model = train(
                    args,
                    model,
                    device,
                    train_loader,
                    optimizer,
                    epoch,
                    loss_fn,
                    vis_params=vis_params,
                )
        except Exception as e:
            if args.websockets:
                warn("An exception occured - restarting websockets")
                try:
                    (
                        train_loader,
                        val_loader,
                        total_L,
                        workers,
                        worker_names,
                    ) = setup_pysyft(args, verbose=cmd_args.verbose)
                except Exception as e:
                    print("restarting failed")
                    raise e
            else:
                raise e

        if (epoch % args.test_interval) == 0:
            _, acc = test(
                args,
                model,
                device,
                val_loader,
                epoch,
                loss_fn,
                num_classes=num_classes,
                vis_params=vis_params,
                class_names=class_names,
            )
            model_path = "model_weights/{:s}_epoch_{:03d}.pt".format(exp_name, epoch,)

            save_model(model, optimizer, model_path, args, epoch)
            accuracies.append(acc)
            model_paths.append(model_path)
    # reversal and formula because we want last occurance of highest value
    accuracies = np.array(accuracies)[::-1]
    am = np.argmax(accuracies)
    highest_acc = len(accuracies) - am - 1
    best_epoch = highest_acc * args.test_interval
    best_model_file = model_paths[highest_acc]
    print(
        "Highest accuracy was {:.1f}% in epoch {:d}".format(accuracies[am], best_epoch)
    )
    # load best model on val set
    state = torch.load(best_model_file, map_location=device)
    model.load_state_dict(state["model_state_dict"])

    """_, result = test(
        args,
        model,
        device,
        val_loader,
        args.epochs + 1,
        loss_fn,
        num_classes=num_classes,
        vis_params=vis_params,
        class_names=class_names,
    )
    print('result: {:.1f} - best accuracy: {:.1f}'.format(result, accuracies[am]))"""
    shutil.copyfile(
        best_model_file, "model_weights/final_{:s}.pt".format(exp_name),
    )
    save_config_results(
        args, accuracies[am], timestamp, "model_weights/completed_trainings.csv"
    )

    # delete old model weights
    for model_file in model_paths:
        remove(model_file)

