# 3.8 は CMake 4 が hard error にする下限。3.16 はこの依存群が実際に必要とする水準。
cmake_minimum_required(VERSION 3.16)
project(kk_can_bringup)

find_package(ament_cmake REQUIRED)

# launch/ と config/ を share/ へ入れる。ビルド成果物は無い（launch とデータのみ）。
install(DIRECTORY launch config DESTINATION share/${PROJECT_NAME})

ament_package()
