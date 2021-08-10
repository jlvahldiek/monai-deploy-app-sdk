# Copyright 2020 - 2021 MONAI Consortium
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from monai.deploy.core import (
    DataPath,
    ExecutionContext,
    Image,
    InputContext,
    IOType,
    Operator,
    OutputContext,
    input,
    output,
)


@input("image", Image, IOType.IN_MEMORY)
@output("image", DataPath, IOType.DISK)
# If `pip_packages` is not specified, the operator will use a default package dependency list (requirements.txt)
# in the same directory (or a parent directory up to App folder, if not exists) of the operator.
# If `pip_packages` is specified, the definition will be aggregated with the package dependency list of other
# operators and the application in packaging time.
# @env(pip_packages=["scikit-image >= 0.18.0"])
class GaussianOperator(Operator):
    """This Operator implements a smoothening based on Gaussian.

    It ingests a single input and provides a single output.
    """

    def compute(self, input: InputContext, output: OutputContext, context: ExecutionContext):
        from skimage.filters import gaussian
        from skimage.io import imsave

        data_in = input.get().asnumpy()
        data_out = gaussian(data_in, sigma=0.2)

        output_folder = output.get().path
        output_folder.mkdir(parents=True, exist_ok=True)
        output_filename = "final_output.png"
        output_path = output_folder / output_filename
        imsave(output_path, data_out)
