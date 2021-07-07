# SAFNet
Implementation of "Similarity-Aware Fusion Network for 3D Semantic Segmentation" IROS 2021

![](./pipeline.png)

## Environment Preparation & Data Preparation

We prepared our environment and [ScanNet data](http://kaldir.vc.in.tum.de/scannet_benchmark/) as follows: 
(Thanks for the detaied instructions of [MVPNet](https://github.com/maxjaritz/mvpnet))

Environment: 

  - Python 3.6
  - Pytorch 1.2.0
  - CUDA 10.0 & CUDNN 7.6.4
 
DATA: 

  - The data is released under the [ScanNet Term of Use](http://kaldir.vc.in.tum.de/scannet/ScanNet_TOS.pdf), please contact ScanNet team for access.
  - See MVPNet repo for processing the raw data and resizing images.

The code is coming soon.

# Citation
If you find our work useful, please cite our [paper](https://arxiv.org/abs/2107.01579):
```
@article{2107.01579,
Author = {Linqing Zhao and Jiwen Lu and Jie Zhou},
Title = {Similarity-Aware Fusion Network for 3D Semantic Segmentation},
Year = {2021},
journal={arXiv preprint arXiv:2107.01579},
}
```
