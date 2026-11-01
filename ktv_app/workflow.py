"""Validated options for the vendored CUDA alignment workflow."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AlignmentOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: Literal["reliable_blocks_tifa"] = "reliable_blocks_tifa"
    vocal_separation: Literal["kim"] = "kim"
    complete_missing: Literal[True] = True
    device: Literal["cuda"] = "cuda"
    vocal_start: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    vocal_end: float | None = Field(default=None, gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def interval(self):
        if (
            self.vocal_start is not None
            and self.vocal_end is not None
            and self.vocal_end <= self.vocal_start
        ):
            raise ValueError("演唱结束必须晚于开始")
        return self
