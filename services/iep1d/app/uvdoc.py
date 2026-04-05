from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as functional

__all__ = [
    "UVDocConfig",
    "UVDocNet",
    "UVDocRectifier",
]


def _conv(
    in_channels: int,
    out_channels: int,
    *,
    kernel_size: int,
    stride: int = 1,
) -> nn.Conv2d:
    return nn.Conv2d(
        in_channels,
        out_channels,
        kernel_size=kernel_size,
        stride=stride,
        padding=kernel_size // 2,
    )


def _dilated_conv(
    in_channels: int,
    out_channels: int,
    *,
    kernel_size: int,
    dilation: int,
    stride: int = 1,
) -> nn.Sequential:
    return nn.Sequential(
        nn.Conv2d(
            in_channels,
            out_channels,
            kernel_size=kernel_size,
            stride=stride,
            padding=dilation * (kernel_size // 2),
            dilation=dilation,
        )
    )


def _dilated_conv_bn_act(
    in_channels: int,
    out_channels: int,
    *,
    activation: nn.Module,
    dilation: int,
) -> nn.Sequential:
    return nn.Sequential(
        nn.Conv2d(
            in_channels,
            out_channels,
            kernel_size=3,
            stride=1,
            padding=dilation,
            dilation=dilation,
            bias=False,
        ),
        nn.BatchNorm2d(out_channels),
        activation,
    )


class ResidualBlockWithDilation(nn.Module):
    def __init__(
        self,
        in_channels: int,
        out_channels: int,
        *,
        kernel_size: int,
        stride: int = 1,
        downsample: nn.Module | None = None,
        is_top: bool = False,
    ) -> None:
        super().__init__()
        self.downsample = downsample
        self.relu = nn.ReLU(inplace=True)
        self.bn1 = nn.BatchNorm2d(out_channels)
        self.bn2 = nn.BatchNorm2d(out_channels)

        if stride != 1 or is_top:
            self.conv1 = _conv(
                in_channels,
                out_channels,
                kernel_size=kernel_size,
                stride=stride,
            )
            self.conv2 = _conv(
                out_channels,
                out_channels,
                kernel_size=kernel_size,
            )
        else:
            self.conv1 = _dilated_conv(
                in_channels,
                out_channels,
                kernel_size=kernel_size,
                dilation=3,
            )
            self.conv2 = _dilated_conv(
                out_channels,
                out_channels,
                kernel_size=kernel_size,
                dilation=3,
            )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        residual = x if self.downsample is None else self.downsample(x)
        out = self.relu(self.bn1(self.conv1(x)))
        out = self.bn2(self.conv2(out))
        out = self.relu(out + residual)
        return out


class ResnetStraight(nn.Module):
    def __init__(
        self,
        *,
        num_filter: int,
        map_num: list[int],
        block_nums: list[int],
        kernel_size: int,
        stride: list[int],
    ) -> None:
        super().__init__()
        self.in_channels = num_filter * map_num[0]
        self.layer1 = self._block_layer(
            num_filter * map_num[0],
            block_nums[0],
            kernel_size=kernel_size,
            stride=stride[0],
        )
        self.layer2 = self._block_layer(
            num_filter * map_num[1],
            block_nums[1],
            kernel_size=kernel_size,
            stride=stride[1],
        )
        self.layer3 = self._block_layer(
            num_filter * map_num[2],
            block_nums[2],
            kernel_size=kernel_size,
            stride=stride[2],
        )

    def _block_layer(
        self,
        out_channels: int,
        block_count: int,
        *,
        kernel_size: int,
        stride: int,
    ) -> nn.Sequential:
        downsample: nn.Module | None = None
        if stride != 1 or self.in_channels != out_channels:
            downsample = nn.Sequential(
                _conv(
                    self.in_channels,
                    out_channels,
                    kernel_size=kernel_size,
                    stride=stride,
                ),
                nn.BatchNorm2d(out_channels),
            )

        layers: list[nn.Module] = [
            ResidualBlockWithDilation(
                self.in_channels,
                out_channels,
                kernel_size=kernel_size,
                stride=stride,
                downsample=downsample,
                is_top=True,
            )
        ]
        self.in_channels = out_channels
        for _ in range(1, block_count):
            layers.append(
                ResidualBlockWithDilation(
                    out_channels,
                    out_channels,
                    kernel_size=kernel_size,
                )
            )
        return nn.Sequential(*layers)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.layer1(x)
        x = self.layer2(x)
        x = self.layer3(x)
        return x


class UVDocNet(nn.Module):
    def __init__(self, *, num_filter: int = 32, kernel_size: int = 5) -> None:
        super().__init__()
        map_num = [1, 2, 4, 8, 16]
        activation = nn.ReLU(inplace=True)
        stride = [1, 2, 2, 2]

        self.resnet_head = nn.Sequential(
            nn.Conv2d(
                3,
                num_filter * map_num[0],
                kernel_size=kernel_size,
                stride=2,
                padding=kernel_size // 2,
                bias=False,
            ),
            nn.BatchNorm2d(num_filter * map_num[0]),
            activation,
            nn.Conv2d(
                num_filter * map_num[0],
                num_filter * map_num[0],
                kernel_size=kernel_size,
                stride=2,
                padding=kernel_size // 2,
                bias=False,
            ),
            nn.BatchNorm2d(num_filter * map_num[0]),
            activation,
        )

        self.resnet_down = ResnetStraight(
            num_filter=num_filter,
            map_num=map_num,
            block_nums=[3, 4, 6, 3],
            kernel_size=kernel_size,
            stride=stride,
        )

        bridge_channels = num_filter * map_num[2]
        self.bridge_1 = _dilated_conv_bn_act(
            bridge_channels,
            bridge_channels,
            activation=activation,
            dilation=1,
        )
        self.bridge_2 = _dilated_conv_bn_act(
            bridge_channels,
            bridge_channels,
            activation=activation,
            dilation=2,
        )
        self.bridge_3 = _dilated_conv_bn_act(
            bridge_channels,
            bridge_channels,
            activation=activation,
            dilation=5,
        )
        self.bridge_4 = nn.Sequential(
            *[
                _dilated_conv_bn_act(
                    bridge_channels,
                    bridge_channels,
                    activation=activation,
                    dilation=dilation,
                )
                for dilation in (8, 3, 2)
            ]
        )
        self.bridge_5 = nn.Sequential(
            *[
                _dilated_conv_bn_act(
                    bridge_channels,
                    bridge_channels,
                    activation=activation,
                    dilation=dilation,
                )
                for dilation in (12, 7, 4)
            ]
        )
        self.bridge_6 = nn.Sequential(
            *[
                _dilated_conv_bn_act(
                    bridge_channels,
                    bridge_channels,
                    activation=activation,
                    dilation=dilation,
                )
                for dilation in (18, 12, 6)
            ]
        )
        self.bridge_concat = nn.Sequential(
            nn.Conv2d(bridge_channels * 6, bridge_channels, kernel_size=1, stride=1, bias=False),
            nn.BatchNorm2d(bridge_channels),
            activation,
        )

        self.out_point_positions_2d = nn.Sequential(
            nn.Conv2d(
                bridge_channels,
                num_filter * map_num[0],
                kernel_size=kernel_size,
                stride=1,
                padding=kernel_size // 2,
                padding_mode="reflect",
                bias=False,
            ),
            nn.BatchNorm2d(num_filter * map_num[0]),
            nn.PReLU(),
            nn.Conv2d(
                num_filter * map_num[0],
                2,
                kernel_size=kernel_size,
                stride=1,
                padding=kernel_size // 2,
                padding_mode="reflect",
            ),
        )
        self.out_point_positions_3d = nn.Sequential(
            nn.Conv2d(
                bridge_channels,
                num_filter * map_num[0],
                kernel_size=kernel_size,
                stride=1,
                padding=kernel_size // 2,
                padding_mode="reflect",
                bias=False,
            ),
            nn.BatchNorm2d(num_filter * map_num[0]),
            nn.PReLU(),
            nn.Conv2d(
                num_filter * map_num[0],
                3,
                kernel_size=kernel_size,
                stride=1,
                padding=kernel_size // 2,
                padding_mode="reflect",
            ),
        )
        self._initialize_weights()

    def _initialize_weights(self) -> None:
        for module in self.modules():
            if isinstance(module, nn.Conv2d):
                nn.init.xavier_normal_(module.weight, gain=0.2)
            if isinstance(module, nn.ConvTranspose2d):
                nn.init.xavier_normal_(module.weight, gain=0.2)

    def forward(self, x: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        head = self.resnet_head(x)
        down = self.resnet_down(head)
        bridge_concat = torch.cat(
            [
                self.bridge_1(down),
                self.bridge_2(down),
                self.bridge_3(down),
                self.bridge_4(down),
                self.bridge_5(down),
                self.bridge_6(down),
            ],
            dim=1,
        )
        bridge = self.bridge_concat(bridge_concat)
        return self.out_point_positions_2d(bridge), self.out_point_positions_3d(bridge)


@dataclass(frozen=True)
class UVDocConfig:
    input_width: int = 488
    input_height: int = 712
    num_filter: int = 32
    kernel_size: int = 5
    remap_chunk_rows: int = 256


def _bilinear_unwarp(
    warped: torch.Tensor,
    point_positions: torch.Tensor,
    *,
    out_width: int,
    out_height: int,
) -> torch.Tensor:
    upsampled_grid = functional.interpolate(
        point_positions,
        size=(out_height, out_width),
        mode="bilinear",
        align_corners=True,
    )
    return functional.grid_sample(
        warped,
        upsampled_grid.permute(0, 2, 3, 1),
        align_corners=True,
    )


class UVDocRectifier:
    def __init__(self, model: UVDocNet, *, device: torch.device, config: UVDocConfig) -> None:
        self._model = model.eval()
        self._device = device
        self._config = config

    @classmethod
    def _remap_checkpoint_keys(cls, state_dict: dict) -> dict:
        """Remap legacy checkpoint keys to current model architecture.

        The checkpoint uses camelCase layer names while the model uses snake_case:
        - out_point_positions2D (checkpoint) → out_point_positions_2d (model)
        - out_point_positions3D (checkpoint) → out_point_positions_3d (model)

        All other keys (including bridge and resnet layers) use the same naming
        convention in both checkpoint and model.
        """
        remapped = {}
        for key, value in state_dict.items():
            new_key = key

            for old, new in (
                ("out_point_positions2D", "out_point_positions_2d"),
                ("out_point_positions3D", "out_point_positions_3d"),
                ("bridge_1.0.0.", "bridge_1.0."),
                ("bridge_1.0.1.", "bridge_1.1."),
                ("bridge_2.0.0.", "bridge_2.0."),
                ("bridge_2.0.1.", "bridge_2.1."),
                ("bridge_3.0.0.", "bridge_3.0."),
                ("bridge_3.0.1.", "bridge_3.1."),
            ):
                new_key = new_key.replace(old, new)

            if new_key in remapped and new_key != key:
                raise RuntimeError(
                    f"UVDoc checkpoint remap collision: {key!r} conflicts with an existing "
                    f"entry for {new_key!r}"
                )
            remapped[new_key] = value

        return remapped

    @classmethod
    def from_checkpoint(
        cls,
        checkpoint_path: str,
        *,
        device: str | torch.device = "cpu",
        config: UVDocConfig | None = None,
    ) -> UVDocRectifier:
        resolved_config = config or UVDocConfig()
        resolved_device = torch.device(device)
        checkpoint = torch.load(checkpoint_path, map_location=resolved_device)
        state_dict = checkpoint
        if isinstance(checkpoint, dict):
            state_dict = checkpoint.get("model_state", checkpoint)
        if not isinstance(state_dict, dict):
            raise RuntimeError(
                "UVDoc checkpoint is malformed: expected a state dict or "
                "a dict containing 'model_state'"
            )

        # Remap legacy checkpoint keys to current architecture
        state_dict = cls._remap_checkpoint_keys(state_dict)

        model = UVDocNet(
            num_filter=resolved_config.num_filter,
            kernel_size=resolved_config.kernel_size,
        )
        model.load_state_dict(state_dict, strict=True)
        model.to(resolved_device)
        model.eval()
        return cls(model, device=resolved_device, config=resolved_config)

    def rectify(self, image_bgr: np.ndarray) -> np.ndarray:
        if image_bgr.ndim != 3 or image_bgr.shape[2] != 3:
            raise ValueError("UVDocRectifier expects an HxWx3 BGR image array")

        model_input = self._resize_for_model(image_bgr)
        model_input_tensor = self._numpy_bgr_to_tensor(model_input).to(self._device)

        with torch.inference_mode():
            point_positions_2d, _ = self._model(model_input_tensor)
            point_positions_2d = point_positions_2d.clamp(-1.0, 1.0).to(
                device="cpu",
                dtype=torch.float32,
            )

        return self._apply_point_position_map(
            image_bgr,
            point_positions_2d,
            chunk_rows=self._config.remap_chunk_rows,
        )

    def _resize_for_model(self, image_bgr: np.ndarray) -> np.ndarray:
        interpolation = cv2.INTER_AREA
        if (
            image_bgr.shape[1] < self._config.input_width
            or image_bgr.shape[0] < self._config.input_height
        ):
            interpolation = cv2.INTER_LINEAR
        return cv2.resize(
            image_bgr,
            (self._config.input_width, self._config.input_height),
            interpolation=interpolation,
        )

    @classmethod
    def _apply_point_position_map(
        cls,
        image_bgr: np.ndarray,
        point_positions_2d: torch.Tensor,
        *,
        chunk_rows: int,
    ) -> np.ndarray:
        if chunk_rows <= 0:
            raise ValueError("chunk_rows must be positive")

        height, width = image_bgr.shape[:2]
        rectified = np.empty_like(image_bgr)
        source_image = np.ascontiguousarray(image_bgr)
        x_coords = cls._normalized_axis(width)

        for row_start in range(0, height, chunk_rows):
            row_end = min(row_start + chunk_rows, height)
            y_coords = cls._normalized_axis(height, start=row_start, stop=row_end)
            grid_y, grid_x = torch.meshgrid(y_coords, x_coords, indexing="ij")
            sampling_grid = torch.stack((grid_x, grid_y), dim=-1).unsqueeze(0)
            stripe_grid = functional.grid_sample(
                point_positions_2d,
                sampling_grid,
                mode="bilinear",
                align_corners=True,
            )[0].numpy()
            map_x = cls._normalized_to_pixel_coordinates(stripe_grid[0], width)
            map_y = cls._normalized_to_pixel_coordinates(stripe_grid[1], height)
            rectified[row_start:row_end] = cv2.remap(
                source_image,
                map_x,
                map_y,
                interpolation=cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_CONSTANT,
            )

        return rectified

    @staticmethod
    def _normalized_axis(length: int, *, start: int = 0, stop: int | None = None) -> torch.Tensor:
        stop = length if stop is None else stop
        if length <= 1:
            return torch.zeros(max(0, stop - start), dtype=torch.float32)
        indices = torch.arange(start, stop, dtype=torch.float32)
        return indices.mul(2.0 / float(length - 1)).sub(1.0)

    @staticmethod
    def _normalized_to_pixel_coordinates(coordinates: np.ndarray, size: int) -> np.ndarray:
        if size <= 1:
            return np.zeros_like(coordinates, dtype=np.float32)
        scale = (size - 1) / 2.0
        return np.ascontiguousarray(((coordinates + 1.0) * scale).astype(np.float32, copy=False))

    @staticmethod
    def _numpy_bgr_to_tensor(image_bgr: np.ndarray) -> torch.Tensor:
        image_rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
        image_rgb = image_rgb.astype(np.float32) / 255.0
        return torch.from_numpy(image_rgb.transpose(2, 0, 1)).unsqueeze(0)

    @staticmethod
    def _tensor_to_numpy_bgr(image_tensor: torch.Tensor) -> np.ndarray:
        image_rgb = image_tensor.squeeze(0).detach().cpu().clamp(0.0, 1.0).numpy()
        image_rgb = (image_rgb.transpose(1, 2, 0) * 255.0).astype(np.uint8)
        return cv2.cvtColor(image_rgb, cv2.COLOR_RGB2BGR)
